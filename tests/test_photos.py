"""Tests for bookinventory.photos: the photo-library fallback for the Simulator.

UIKit is simulated through a fake ``rubicon.objc`` module, the same way the
Vision bridge is tested. What's verified is the exact sequence of UIKit calls
and that the delegate callbacks resolve the awaited result.
"""

import sys
import types

import pytest

from bookinventory import photos


class FakeObjC:
    """A fake ``rubicon.objc`` whose UIImagePickerController records everything."""

    def __init__(self, camera=False):
        self.camera = camera
        self.calls = []
        objc = self

        class NSObject:
            _instances = []

            @classmethod
            def new(cls):
                instance = cls()
                cls._instances.append(instance)
                return instance

        class Picker:
            def __init__(self):
                self.sourceType = None
                self.delegate = None

            @classmethod
            def new(cls):
                picker = cls()
                objc.calls.append(("new picker", picker))
                return picker

            @classmethod
            def isSourceTypeAvailable(cls, source):
                objc.calls.append(("isSourceTypeAvailable", source))
                return objc.camera if source == photos.SOURCE_CAMERA else True

            def dismissViewControllerAnimated(self, animated, completion):
                objc.calls.append(("dismiss", animated, completion))

        self.NSObject = NSObject
        self.Picker = Picker
        self.module = types.ModuleType("rubicon.objc")
        self.module.ObjCClass = lambda name: {"UIImagePickerController": Picker}[name]
        self.module.NSObject = NSObject
        self.module.objc_method = lambda fn: fn

    def install(self, monkeypatch):
        rubicon = types.ModuleType("rubicon")
        rubicon.objc = self.module
        monkeypatch.setitem(sys.modules, "rubicon", rubicon)
        monkeypatch.setitem(sys.modules, "rubicon.objc", self.module)
        monkeypatch.setattr(photos, "_delegate_class", None)  # fresh class per test


class FakeWindow:
    def __init__(self):
        self.presented = []
        controller = self

        class RootViewController:
            def presentViewController(self, picker, animated, completion):
                controller.presented.append((picker, animated, completion))

        class Native:
            rootViewController = RootViewController()

        class Impl:
            native = Native()

        self._impl = Impl()


class TestCameraAvailable:
    def test_off_ios_defers_to_toga(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert photos.camera_available() is True

    @pytest.mark.parametrize("camera", [True, False])
    def test_on_ios_asks_uikit(self, monkeypatch, camera):
        monkeypatch.setattr(sys, "platform", "ios")
        objc = FakeObjC(camera=camera)
        objc.install(monkeypatch)
        assert photos.camera_available() is camera
        assert objc.calls == [("isSourceTypeAvailable", photos.SOURCE_CAMERA)]


class TestPickPhoto:
    async def test_presents_library_picker_and_returns_image(self, monkeypatch):
        import asyncio

        objc = FakeObjC()
        objc.install(monkeypatch)
        # The delegate wraps the native UIImage in a toga.Image; stub that so the
        # test doesn't depend on which Toga backend is installed.
        monkeypatch.setattr(photos.toga, "Image", lambda native: ("toga.Image", native))
        window = FakeWindow()

        task = asyncio.ensure_future(photos.pick_photo(window))
        await asyncio.sleep(0)  # let pick_photo run up to its await

        [(picker, animated, completion)] = window.presented
        assert picker.sourceType == photos.SOURCE_PHOTO_LIBRARY
        assert animated is True and completion is None
        assert picker.delegate is not None
        assert not task.done()

        # The user picks a photo: UIKit calls the delegate with the image.
        picker.delegate.imagePickerController_didFinishPickingMediaWithInfo_(
            picker, {photos.ORIGINAL_IMAGE_KEY: "native-uiimage"}
        )
        assert await task == ("toga.Image", "native-uiimage")
        assert ("dismiss", True, None) in objc.calls

    async def test_cancel_returns_none(self, monkeypatch):
        import asyncio

        objc = FakeObjC()
        objc.install(monkeypatch)
        window = FakeWindow()
        task = asyncio.ensure_future(photos.pick_photo(window))
        await asyncio.sleep(0)
        [(picker, _, _)] = window.presented
        picker.delegate.imagePickerControllerDidCancel_(picker)
        assert await task is None
        assert ("dismiss", True, None) in objc.calls

    async def test_duplicate_callbacks_are_harmless(self, monkeypatch):
        import asyncio

        objc = FakeObjC()
        objc.install(monkeypatch)
        window = FakeWindow()
        task = asyncio.ensure_future(photos.pick_photo(window))
        await asyncio.sleep(0)
        [(picker, _, _)] = window.presented
        picker.delegate.imagePickerControllerDidCancel_(picker)
        picker.delegate.imagePickerControllerDidCancel_(picker)  # second call ignored
        assert await task is None

    def test_delegate_class_is_created_once(self, monkeypatch):
        objc = FakeObjC()
        objc.install(monkeypatch)
        first = photos._picker_delegate_class()
        second = photos._picker_delegate_class()
        assert first is second
        assert issubclass(first, objc.NSObject)


class FakePhotosUI:
    """Simulates PhotosUI: PHPickerViewController, configuration, results and item providers."""

    def __init__(self, available=True):
        self.available = available
        self.calls = []
        kit = self

        class NSObject:
            @classmethod
            def new(cls):
                return cls()

        class Configuration:
            def __init__(self):
                self.selectionLimit = None
                self.filter = None

            @classmethod
            def alloc(cls):
                return cls

            @classmethod
            def init(cls):
                return cls()

        class Filter:
            imagesFilter = "images-filter"

        class Picker:
            def __init__(self):
                self.delegate = None

            @classmethod
            def alloc(cls):
                return cls

            @classmethod
            def initWithConfiguration(cls, configuration):
                picker = cls()
                picker.configuration = configuration
                kit.calls.append(("picker", configuration))
                return picker

            def dismissViewControllerAnimated(self, animated, completion):
                kit.calls.append(("dismiss", animated))

        class ItemProvider:
            def __init__(self, image):
                self.image = image

            def canLoadObjectOfClass(self, cls):
                return self.image is not None

            def loadObjectOfClass(self, cls, completionHandler):
                kit.calls.append(("load", cls))
                # Simulate the asynchronous completion on a background thread.
                import threading

                threading.Thread(
                    target=completionHandler.fn, args=(self.image, None), daemon=True
                ).start()

        class Result:
            def __init__(self, image):
                self.itemProvider = ItemProvider(image)

        class UIImage:
            pass

        def objc_class(name):
            if not kit.available and name.startswith("PHPicker"):
                raise NameError(name)
            return {
                "PHPickerConfiguration": Configuration,
                "PHPickerViewController": Picker,
                "PHPickerFilter": Filter,
                "UIImage": UIImage,
                "UIImagePickerController": LegacyPicker,
            }[name]

        class LegacyPicker:
            def __init__(self):
                self.sourceType = None
                self.delegate = None

            @classmethod
            def new(cls):
                picker = cls()
                kit.calls.append(("legacy picker", picker))
                return picker

            def dismissViewControllerAnimated(self, animated, completion):
                kit.calls.append(("dismiss", animated))

        class Block:
            def __init__(self, fn, restype, *argtypes):
                self.fn = fn

        self.Result = Result
        self.module = types.ModuleType("rubicon.objc")
        self.module.ObjCClass = objc_class
        self.module.NSObject = NSObject
        self.module.objc_method = lambda fn: fn
        self.module.Block = Block
        self.module.ObjCInstance = lambda ptr: ptr
        self.module.objc_id = object

    def install(self, monkeypatch):
        rubicon = types.ModuleType("rubicon")
        rubicon.objc = self.module
        monkeypatch.setitem(sys.modules, "rubicon", rubicon)
        monkeypatch.setitem(sys.modules, "rubicon.objc", self.module)
        monkeypatch.setattr(photos, "_ph_delegate_class", None)
        monkeypatch.setattr(photos, "_delegate_class", None)
        monkeypatch.setattr(photos.ctypes, "CDLL", lambda path: None)
        monkeypatch.setattr(photos.toga, "Image", lambda native: ("toga.Image", native))


class TestPickPhotos:
    async def test_multi_select_returns_all_images(self, monkeypatch):
        import asyncio

        kit = FakePhotosUI()
        kit.install(monkeypatch)
        window = FakeWindow()
        task = asyncio.ensure_future(photos.pick_photos(window))
        await asyncio.sleep(0)
        [(picker, animated, completion)] = window.presented
        assert picker.configuration.selectionLimit == 0  # unlimited
        assert picker.configuration.filter == "images-filter"
        assert picker.delegate is not None

        picker.delegate.picker_didFinishPicking_(
            picker, [kit.Result("img-1"), kit.Result(None), kit.Result("img-2")]
        )
        images = await asyncio.wait_for(task, 2)
        assert sorted(images) == [("toga.Image", "img-1"), ("toga.Image", "img-2")]
        assert ("dismiss", True) in kit.calls
        assert kit.calls.count(("load", kit.module.ObjCClass("UIImage"))) == 2

    async def test_cancel_returns_empty(self, monkeypatch):
        import asyncio

        kit = FakePhotosUI()
        kit.install(monkeypatch)
        window = FakeWindow()
        task = asyncio.ensure_future(photos.pick_photos(window))
        await asyncio.sleep(0)
        [(picker, _, _)] = window.presented
        picker.delegate.picker_didFinishPicking_(picker, [])
        assert await asyncio.wait_for(task, 2) == []

    async def test_limit_is_passed_through(self, monkeypatch):
        import asyncio

        kit = FakePhotosUI()
        kit.install(monkeypatch)
        window = FakeWindow()
        task = asyncio.ensure_future(photos.pick_photos(window, limit=3))
        await asyncio.sleep(0)
        [(picker, _, _)] = window.presented
        assert picker.configuration.selectionLimit == 3
        picker.delegate.picker_didFinishPicking_(picker, [])
        await task

    async def test_falls_back_to_legacy_picker(self, monkeypatch):
        import asyncio

        kit = FakePhotosUI(available=False)
        kit.install(monkeypatch)
        window = FakeWindow()
        task = asyncio.ensure_future(photos.pick_photos(window))
        await asyncio.sleep(0)
        [(picker, _, _)] = window.presented
        assert ("legacy picker", picker) in kit.calls
        picker.delegate.imagePickerController_didFinishPickingMediaWithInfo_(
            picker, {photos.ORIGINAL_IMAGE_KEY: "legacy-image"}
        )
        assert await task == [("toga.Image", "legacy-image")]

    async def test_legacy_fallback_cancel(self, monkeypatch):
        import asyncio

        kit = FakePhotosUI(available=False)
        kit.install(monkeypatch)
        window = FakeWindow()
        task = asyncio.ensure_future(photos.pick_photos(window))
        await asyncio.sleep(0)
        [(picker, _, _)] = window.presented
        picker.delegate.imagePickerControllerDidCancel_(picker)
        assert await task == []


class TestChoosePhotos:
    async def test_ios(self, monkeypatch):
        monkeypatch.setattr(photos, "is_ios", lambda: True)

        async def pick_photos(window, limit=0):
            return ["a", "b"]

        monkeypatch.setattr(photos, "pick_photos", pick_photos)
        assert await photos.choose_photos("w") == ["a", "b"]

    async def test_desktop(self, monkeypatch):
        monkeypatch.setattr(photos, "is_ios", lambda: False)

        async def pick_files(window):
            return ["file"]

        monkeypatch.setattr(photos, "pick_files", pick_files)
        assert await photos.choose_photos("w") == ["file"]
