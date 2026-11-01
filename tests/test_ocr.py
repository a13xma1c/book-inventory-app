"""Tests for bookinventory.ocr.

Vision only exists on Apple platforms, so the Objective-C side is simulated:
a fake ``rubicon.objc`` module whose classes record every call. This checks
the *way* we drive Vision (orientations, request options, result handling),
which is exactly the part that can't be verified by eye.
"""

import sys
import types
from types import SimpleNamespace

import pytest

from bookinventory import ocr
from bookinventory.ocr import OcrLine


class Candidate:
    def __init__(self, string, confidence=0.9):
        self.string, self.confidence = string, confidence


class Box:
    def __init__(self, x=0.1, y=0.2, width=0.3, height=0.05):
        self.origin = SimpleNamespace(x=x, y=y)
        self.size = SimpleNamespace(width=width, height=height)


class Observation:
    def __init__(self, *candidates, box=None):
        self._candidates = list(candidates)
        self.boundingBox = box or Box()

    def topCandidates(self, n):
        return self._candidates[:n]


class FakeVision:
    """Builds the fake ObjC classes and records what the code does with them."""

    def __init__(self, results_by_orientation, perform_ok=True, decodes=True):
        self.results_by_orientation = results_by_orientation
        self.perform_ok = perform_ok
        self.decodes = decodes
        self.calls = []
        vision = self

        class NSData:
            @classmethod
            def dataWithBytes(cls, data, length):
                vision.calls.append(("dataWithBytes", data, length))
                return ("nsdata", data)

        class UIImage:
            CGImage = "cgimage"

            @classmethod
            def imageWithData(cls, data):
                vision.calls.append(("imageWithData", data))
                return cls() if vision.decodes else None

        self.UIImage = UIImage

        class Request:
            def __init__(self):
                self.recognitionLevel = None
                self.usesLanguageCorrection = None
                self.automaticallyDetectsLanguage = None
                self.results = None

            @classmethod
            def alloc(cls):
                return cls

            @classmethod
            def init(cls):
                request = cls()
                vision.calls.append(("request", request))
                return request

        class Handler:
            @classmethod
            def alloc(cls):
                return cls

            @classmethod
            def initWithCGImage(cls, image, orientation, options):
                handler = cls()
                handler.orientation = orientation
                vision.calls.append(("handler", image, orientation, options))
                return handler

            def performRequests(self, requests, error):
                vision.calls.append(("perform", self.orientation, requests, error))
                if vision.perform_ok:
                    requests[0].results = vision.results_by_orientation.get(self.orientation)
                return vision.perform_ok

        self.classes = {
            "NSData": NSData,
            "UIImage": UIImage,
            "VNImageRequestHandler": Handler,
            "VNRecognizeTextRequest": Request,
        }

    def install(self, monkeypatch):
        rubicon = types.ModuleType("rubicon")
        objc = types.ModuleType("rubicon.objc")
        objc.ObjCClass = lambda name: self.classes[name]
        rubicon.objc = objc
        monkeypatch.setitem(sys.modules, "rubicon", rubicon)
        monkeypatch.setitem(sys.modules, "rubicon.objc", objc)
        loaded = []
        monkeypatch.setattr(ocr.ctypes, "CDLL", lambda path: loaded.append(path))
        return loaded


@pytest.fixture
def vision(monkeypatch):
    fake = FakeVision(
        {
            1: [Observation(Candidate("PENGUIN", 0.6))],
            6: [
                Observation(Candidate("THE HOBBIT", 0.95), Candidate("THE HOBBJT", 0.4)),
                Observation(),  # no candidates
                Observation(Candidate("DUNE", 0.9), box=Box(0.5, 0.6, 0.2, 0.04)),
                Observation(Candidate("smudge", 0.1)),  # below MIN_CONFIDENCE
            ],
            8: None,  # Vision can return nil results
        }
    )
    fake.loaded = fake.install(monkeypatch)
    return fake


class TestConstants:
    def test_orientations_are_up_right_left(self):
        # CGImagePropertyOrientation: up=1, right=6, left=8
        assert ocr._ORIENTATIONS == (1, 6, 8)

    def test_accurate_recognition_level(self):
        assert ocr._RECOGNITION_LEVEL_ACCURATE == 0  # VNRequestTextRecognitionLevelAccurate

    def test_vision_framework_path(self):
        assert ocr._VISION_PATH.endswith("Vision.framework/Vision")


class TestRecognizeText:
    def test_returns_top_candidate_per_observation_across_orientations(self, vision):
        assert ocr.recognize_text(b"png-bytes") == ["PENGUIN", "THE HOBBIT", "DUNE"]

    def test_loads_vision_framework(self, vision):
        ocr.recognize_text(b"png-bytes")
        assert vision.loaded == [ocr._VISION_PATH]

    def test_image_is_wrapped_in_nsdata_once(self, vision):
        ocr.recognize_text(b"png-bytes")
        assert vision.calls.count(("dataWithBytes", b"png-bytes", 9)) == 1
        assert vision.calls.count(("imageWithData", ("nsdata", b"png-bytes"))) == 1

    def test_one_handler_per_orientation_on_the_same_image(self, vision):
        ocr.recognize_text(b"png-bytes")
        handlers = [c for c in vision.calls if c[0] == "handler"]
        assert handlers == [
            ("handler", "cgimage", 1, None),
            ("handler", "cgimage", 6, None),
            ("handler", "cgimage", 8, None),
        ]

    def test_request_options(self, vision):
        ocr.recognize_text(b"png-bytes")
        requests = [c[1] for c in vision.calls if c[0] == "request"]
        assert len(requests) == 3
        for request in requests:
            assert request.recognitionLevel == ocr._RECOGNITION_LEVEL_ACCURATE
            assert request.usesLanguageCorrection is True
            assert request.automaticallyDetectsLanguage is True

    def test_recognize_lines_keeps_geometry_confidence_and_orientation(self, vision):
        lines = ocr.recognize_lines(b"png-bytes")
        assert [line.text for line in lines] == ["PENGUIN", "THE HOBBIT", "DUNE"]
        dune = lines[2]
        assert dune == OcrLine("DUNE", 0.9, 6, 0.5, 0.6, 0.2, 0.04)
        assert lines[0].orientation == 1 and lines[1].orientation == 6

    def test_low_confidence_reads_are_dropped(self, vision):
        assert "smudge" not in ocr.recognize_text(b"png-bytes")
        assert ocr.MIN_CONFIDENCE == 0.3

    def test_native_image_skips_decoding(self, vision):
        native = vision.UIImage()
        lines = ocr.recognize_lines(native_image=native)
        assert [line.text for line in lines] == ["PENGUIN", "THE HOBBIT", "DUNE"]
        assert not any(c[0] in ("dataWithBytes", "imageWithData") for c in vision.calls)

    def test_each_request_performed_with_nil_error(self, vision):
        ocr.recognize_text(b"png-bytes")
        performs = [c for c in vision.calls if c[0] == "perform"]
        assert [c[1] for c in performs] == [1, 6, 8]
        for _, _, requests, error in performs:
            assert len(requests) == 1
            assert error is None

    def test_nil_results_are_tolerated(self, vision):
        vision.results_by_orientation = {1: None, 6: None, 8: None}
        assert ocr.recognize_text(b"png-bytes") == []

    def test_failed_perform_yields_nothing(self, vision):
        vision.perform_ok = False
        assert ocr.recognize_text(b"png-bytes") == []

    def test_duplicates_across_orientations_are_kept(self, vision):
        # Deduplication is matching.candidate_queries' job, not OCR's.
        vision.results_by_orientation = {
            1: [Observation(Candidate("DUNE"))],
            6: [Observation(Candidate("DUNE"))],
        }
        assert ocr.recognize_text(b"png-bytes") == ["DUNE", "DUNE"]

    def test_strings_are_coerced_to_python_str(self, vision):
        class NSStringLike:
            def __str__(self):
                return "Emma"

        vision.results_by_orientation = {1: [Observation(Candidate(NSStringLike()))]}
        result = ocr.recognize_text(b"png-bytes")
        assert result == ["Emma"]
        assert all(type(line) is str for line in result)

    def test_undecodable_image_raises(self, vision):
        vision.decodes = False
        with pytest.raises(AttributeError):
            ocr.recognize_text(b"not an image")

    def test_without_rubicon_raises_import_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "rubicon", None)
        monkeypatch.setitem(sys.modules, "rubicon.objc", None)
        monkeypatch.setattr(ocr.ctypes, "CDLL", lambda path: None)
        with pytest.raises(ImportError):
            ocr.recognize_text(b"png-bytes")


def test_recognize_lines_needs_an_image(vision):
    with pytest.raises(ValueError):
        ocr.recognize_lines()
