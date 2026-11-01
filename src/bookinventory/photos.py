"""Getting an existing photo: the photo library on iOS, a file dialog elsewhere.

Toga has no photo-library API. On iOS this module presents the standard
picker (``UIImagePickerController`` with ``sourceType = PhotoLibrary``),
implemented with Rubicon exactly the way toga-iOS implements its camera
picker. On desktop platforms (``briefcase dev``) it falls back to a file-open
dialog.

It also answers whether a camera exists at all: Toga's iOS camera support
silently returns ``None`` from ``take_photo()`` when there is none - which is
always the case in the Simulator - so the app asks first and offers the
library instead. Drag a bookshelf photo into the Simulator's Photos app and
the whole scan flow can be exercised without a device.
"""

import asyncio
import ctypes
import sys

import toga

# UIImagePickerControllerSourceType
SOURCE_PHOTO_LIBRARY = 0
SOURCE_CAMERA = 1

# Key in the ``info`` dictionary handed to the picker delegate.
ORIGINAL_IMAGE_KEY = "UIImagePickerControllerOriginalImage"

IMAGE_TYPES = ["png", "jpg", "jpeg", "heic", "heif", "tiff", "bmp"]

_PHOTOS_UI_PATH = "/System/Library/Frameworks/PhotosUI.framework/PhotosUI"


def is_ios() -> bool:
    return sys.platform == "ios"


def camera_available() -> bool:
    """Whether a camera can be used here.

    Off iOS this is always ``True`` - Toga's own camera support (or its test
    backend) is in charge there. On iOS it asks UIKit, which says ``False`` in
    the Simulator.
    """
    if not is_ios():
        return True
    from rubicon.objc import ObjCClass

    picker_class = ObjCClass("UIImagePickerController")
    return bool(picker_class.isSourceTypeAvailable(SOURCE_CAMERA))


_delegate_class = None


def _picker_delegate_class():
    """Create (once) the Objective-C delegate class that receives picker events.

    Objective-C classes are registered globally by name, so this must only
    ever run once per process - hence the cache.
    """
    global _delegate_class
    if _delegate_class is None:
        from rubicon.objc import NSObject, objc_method

        class BookInventoryPickerDelegate(NSObject):
            @objc_method
            def imagePickerController_didFinishPickingMediaWithInfo_(self, picker, info) -> None:
                picker.dismissViewControllerAnimated(True, completion=None)
                self.on_done(toga.Image(info[ORIGINAL_IMAGE_KEY]))

            @objc_method
            def imagePickerControllerDidCancel_(self, picker) -> None:
                picker.dismissViewControllerAnimated(True, completion=None)
                self.on_done(None)

        _delegate_class = BookInventoryPickerDelegate
    return _delegate_class


async def pick_photo(window: toga.Window) -> "toga.Image | None":
    """Show the photo library and return the chosen image, or ``None`` if cancelled."""
    from rubicon.objc import ObjCClass

    future = asyncio.get_running_loop().create_future()

    def done(image):
        if not future.done():
            future.set_result(image)

    picker = ObjCClass("UIImagePickerController").new()
    picker.sourceType = SOURCE_PHOTO_LIBRARY
    delegate = _picker_delegate_class().new()
    delegate.on_done = done
    picker.delegate = delegate  # weak reference: ``delegate`` must outlive the picker

    window._impl.native.rootViewController.presentViewController(
        picker, animated=True, completion=None
    )
    try:
        return await future
    finally:
        # Keep the picker and its delegate alive until the picker has been dismissed.
        del picker, delegate


_ph_delegate_class = None


def _ph_picker_delegate_class():
    """The ``PHPickerViewControllerDelegate`` class (created once)."""
    global _ph_delegate_class
    if _ph_delegate_class is None:
        from rubicon.objc import NSObject, objc_method

        class BookInventoryPHPickerDelegate(NSObject):
            @objc_method
            def picker_didFinishPicking_(self, picker, results) -> None:
                picker.dismissViewControllerAnimated(True, completion=None)
                self.on_done(results)

        _ph_delegate_class = BookInventoryPHPickerDelegate
    return _ph_delegate_class


async def pick_photos(window: toga.Window, limit: int = 0) -> list:
    """Choose one or more photos with the modern ``PHPickerViewController``.

    No photo-library permission is needed (the picker runs out of process) and
    several photos of one shelf can be chosen at once. ``limit=0`` means
    unlimited. Returns the chosen images; empty if the user cancelled. Falls
    back to :func:`pick_photo` where PhotosUI isn't available.
    """
    from rubicon.objc import ObjCClass

    try:
        ctypes.CDLL(_PHOTOS_UI_PATH)
        PHPickerConfiguration = ObjCClass("PHPickerConfiguration")
        PHPickerViewController = ObjCClass("PHPickerViewController")
        PHPickerFilter = ObjCClass("PHPickerFilter")
    except (OSError, NameError, ValueError):
        image = await pick_photo(window)
        return [] if image is None else [image]

    configuration = PHPickerConfiguration.alloc().init()
    configuration.selectionLimit = limit
    configuration.filter = PHPickerFilter.imagesFilter
    picker = PHPickerViewController.alloc().initWithConfiguration(configuration)

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    delegate = _ph_picker_delegate_class().new()

    def done(results) -> None:
        if not future.done():
            future.set_result(results)

    delegate.on_done = done
    picker.delegate = delegate

    window._impl.native.rootViewController.presentViewController(
        picker, animated=True, completion=None
    )
    try:
        results = await future
        return await load_picked_images(results)
    finally:
        del picker, delegate


async def load_picked_images(results) -> list:
    """Load the ``UIImage`` for each ``PHPickerResult`` (the loads are asynchronous)."""
    from rubicon.objc import Block, ObjCClass, ObjCInstance, objc_id

    UIImage = ObjCClass("UIImage")
    loop = asyncio.get_running_loop()
    images: list = []
    blocks = []  # keep the completion blocks alive until they've run
    pending = [r for r in results if r.itemProvider.canLoadObjectOfClass(UIImage)]
    if not pending:
        return images
    done = loop.create_future()
    remaining = len(pending)

    def collect(image):
        nonlocal remaining
        if image is not None:
            images.append(toga.Image(image))
        remaining -= 1
        if remaining == 0 and not done.done():
            done.set_result(None)

    for result in pending:

        def completion(image_ptr, error_ptr) -> None:  # called on a background thread
            image = ObjCInstance(image_ptr) if image_ptr else None
            loop.call_soon_threadsafe(collect, image)

        block = Block(completion, None, objc_id, objc_id)
        blocks.append(block)
        result.itemProvider.loadObjectOfClass(UIImage, completionHandler=block)
    await done
    return images


async def pick_files(window: toga.Window) -> list:
    """Desktop stand-in for the multi-select photo picker."""
    paths = await window.dialog(
        toga.OpenFileDialog(
            "Choose photos of a bookshelf", file_types=IMAGE_TYPES, multiple_select=True
        )
    )
    return [toga.Image(path) for path in paths or []]


async def choose_photos(window: toga.Window) -> list:
    """One or more existing photos chosen by the user (empty if cancelled)."""
    if is_ios():
        return await pick_photos(window)
    return await pick_files(window)
