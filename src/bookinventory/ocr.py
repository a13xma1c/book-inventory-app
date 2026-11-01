"""On-device text recognition using Apple's Vision framework (iOS only).

Book spines are usually printed sideways, so the photo is analysed three times:
upright, rotated clockwise and rotated counter-clockwise. Vision runs entirely
on the device; no image leaves the phone.

:func:`recognize_lines` returns every line with its confidence and bounding
box (so lines can be grouped per spine, see :mod:`bookinventory.spines`);
:func:`recognize_text` is the plain-strings convenience wrapper.
"""

import ctypes
from dataclasses import dataclass

# CGImagePropertyOrientation values
_ORIENTATIONS = (1, 6, 8)  # up, right, left
_VISION_PATH = "/System/Library/Frameworks/Vision.framework/Vision"
_RECOGNITION_LEVEL_ACCURATE = 0

#: Vision's per-line confidence below which a read is discarded as noise.
MIN_CONFIDENCE = 0.3


@dataclass(frozen=True)
class OcrLine:
    """A line of recognised text.

    The box is Vision's normalised bounding box (0-1, origin bottom-left) in
    the coordinate space of the *oriented* image, so lines from the same
    orientation can be compared geometrically.
    """

    text: str
    confidence: float = 1.0
    orientation: int = 1
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0


def _cg_image(image_bytes: bytes | None, native_image):
    """A CGImage for Vision, decoding ``image_bytes`` only if no native UIImage is given."""
    from rubicon.objc import ObjCClass

    if native_image is None:
        if image_bytes is None:
            raise ValueError("recognize_lines needs image bytes or a native image")
        NSData = ObjCClass("NSData")
        UIImage = ObjCClass("UIImage")
        native_image = UIImage.imageWithData(
            NSData.dataWithBytes(image_bytes, length=len(image_bytes))
        )
    return native_image.CGImage


def recognize_lines(image_bytes: bytes | None = None, native_image=None) -> list[OcrLine]:
    """Every line of text Vision finds, in all three orientations.

    :param image_bytes: an encoded image (PNG/JPEG), used if no native image is given.
    :param native_image: a ``UIImage`` - avoids re-encoding a camera photo as PNG.
    """
    from rubicon.objc import ObjCClass

    ctypes.CDLL(_VISION_PATH)
    VNImageRequestHandler = ObjCClass("VNImageRequestHandler")
    VNRecognizeTextRequest = ObjCClass("VNRecognizeTextRequest")
    cg_image = _cg_image(image_bytes, native_image)

    lines: list[OcrLine] = []
    for orientation in _ORIENTATIONS:
        request = VNRecognizeTextRequest.alloc().init()
        request.recognitionLevel = _RECOGNITION_LEVEL_ACCURATE
        request.usesLanguageCorrection = True
        try:
            request.automaticallyDetectsLanguage = True  # iOS 16+
        except AttributeError:  # pragma: no cover - older iOS
            pass
        handler = VNImageRequestHandler.alloc().initWithCGImage(
            cg_image, orientation=orientation, options=None
        )
        handler.performRequests([request], error=None)
        for observation in request.results or []:
            candidates = observation.topCandidates(1)
            if not candidates:
                continue
            candidate = candidates[0]
            confidence = float(candidate.confidence)
            if confidence < MIN_CONFIDENCE:
                continue
            box = observation.boundingBox
            lines.append(
                OcrLine(
                    text=str(candidate.string),
                    confidence=confidence,
                    orientation=orientation,
                    x=float(box.origin.x),
                    y=float(box.origin.y),
                    width=float(box.size.width),
                    height=float(box.size.height),
                )
            )
    return lines


def recognize_text(image_bytes: bytes) -> list[str]:
    """Return every line of text Vision finds in the encoded image (PNG/JPEG)."""
    return [line.text for line in recognize_lines(image_bytes)]
