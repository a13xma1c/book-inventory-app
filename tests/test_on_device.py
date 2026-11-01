"""Smoke tests that only run on a real iOS runtime (``briefcase run iOS --test``).

The Objective-C bridges (``ocr.py``, ``photos.py``, ``glass.py``) are unit
tested against simulated APIs everywhere else; here, with the real runtime
available, we check that every class and selector they rely on actually
exists. A typo in a selector name would otherwise only show up as a crash
the first time a user taps the button.
"""

import sys

import pytest

if sys.platform != "ios":
    pytest.skip("needs the iOS runtime", allow_module_level=True)

import ctypes  # noqa: E402

from rubicon.objc import SEL, ObjCClass  # noqa: E402

from bookinventory import ocr, photos  # noqa: E402

# (class, instance selectors, class selectors)
EXPECTATIONS = [
    ("NSData", [], ["dataWithBytes:length:"]),
    ("UIImage", ["CGImage"], ["imageWithData:", "systemImageNamed:"]),
    (
        "VNRecognizeTextRequest",
        ["setRecognitionLevel:", "setUsesLanguageCorrection:", "results"],
        [],
    ),
    (
        "VNImageRequestHandler",
        ["initWithCGImage:orientation:options:", "performRequests:error:"],
        [],
    ),
    ("VNRecognizedTextObservation", ["topCandidates:", "boundingBox"], []),
    ("VNRecognizedText", ["string", "confidence"], []),
    ("UIImagePickerController", ["setSourceType:", "setDelegate:"], ["isSourceTypeAvailable:"]),
    ("PHPickerConfiguration", ["setSelectionLimit:", "setFilter:"], []),
    ("PHPickerViewController", ["initWithConfiguration:", "setDelegate:"], []),
    ("PHPickerFilter", [], ["imagesFilter"]),
    ("NSItemProvider", ["canLoadObjectOfClass:", "loadObjectOfClass:completionHandler:"], []),
    (
        "UIBarButtonItem",
        ["initWithImage:style:target:action:", "initWithBarButtonSystemItem:target:action:"],
        [],
    ),
    ("UINavigationController", ["setToolbarHidden:animated:"], []),
    (
        "UIViewController",
        [
            "setToolbarItems:animated:",
            "navigationItem",
            "presentViewController:animated:completion:",
        ],
        [],
    ),
    ("UIActivityViewController", ["initWithActivityItems:applicationActivities:"], []),
    ("NSURL", [], ["fileURLWithPath:"]),
    ("UIColor", [], ["secondaryLabelColor"]),
]


@pytest.fixture(scope="module", autouse=True)
def frameworks():
    ctypes.CDLL(ocr._VISION_PATH)
    ctypes.CDLL(photos._PHOTOS_UI_PATH)


@pytest.mark.parametrize("name, instance_selectors, class_selectors", EXPECTATIONS)
def test_classes_and_selectors_exist(name, instance_selectors, class_selectors):
    cls = ObjCClass(name)
    for selector in instance_selectors:
        assert cls.instancesRespondToSelector(SEL(selector)), f"-[{name} {selector}]"
    for selector in class_selectors:
        assert cls.respondsToSelector(SEL(selector)), f"+[{name} {selector}]"


def test_vision_recognises_nothing_in_a_blank_image():
    """End-to-end through Vision with a tiny generated image: no crash, no text."""
    from rubicon.objc import Block, CGSize, objc_id

    renderer = ObjCClass("UIGraphicsImageRenderer").alloc().initWithSize(CGSize(64, 64))
    image = renderer.imageWithActions(Block(lambda context: None, None, objc_id))
    assert ocr.recognize_lines(native_image=image) == []


def test_camera_availability_is_answered():
    assert photos.camera_available() in (True, False)
