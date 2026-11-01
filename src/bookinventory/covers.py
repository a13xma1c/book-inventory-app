"""Download and cache book cover thumbnails from Open Library.

Covers are purely cosmetic, so everything here is best-effort: a failed
download is simply a row without a picture. Files are cached under the app's
cache directory by cover id.

Note on the file extension: the covers are JPEGs, but Toga's icon loader
only probes ``.png``/``.ico``/... names while the underlying image decoders
(UIImage, NSImage, Pillow) sniff the real format - so the JPEG bytes are
stored under a ``.png`` name and loaded with ``toga.Icon(path_without_suffix)``.
"""

from __future__ import annotations

import re
import urllib.request
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bookinventory.database import Book
from bookinventory.lookup import USER_AGENT

_COVER_ID = re.compile(r"/b/id/(\d+)-")
Download = Callable[[str], bytes]


def download(url: str, timeout: float = 10.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def cover_id(url: str) -> str | None:
    match = _COVER_ID.search(url or "")
    return match.group(1) if match else None


def cover_base(cache_dir: Path, book: Book) -> Path | None:
    """Path (without suffix) of the cached cover for ``book``, or ``None`` if it has none."""
    identifier = cover_id(book.cover_url)
    return None if identifier is None else Path(cache_dir) / identifier


def cached_cover(cache_dir: Path, book: Book) -> Path | None:
    """The cover's base path if it is already on disk."""
    base = cover_base(cache_dir, book)
    return base if base is not None and base.with_suffix(".png").exists() else None


def fetch_cover(cache_dir: Path, book: Book, download: Download = download) -> Path | None:
    """Download the cover if needed. Returns the base path, or ``None`` on failure."""
    base = cover_base(cache_dir, book)
    if base is None:
        return None
    target = base.with_suffix(".png")
    if target.exists():
        return base
    try:
        data = download(book.cover_url)
    except Exception:  # noqa: BLE001 - best effort
        return None
    if not data:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    return base


def fetch_covers(
    cache_dir: Path, books: Iterable[Book], download: Download = download, workers: int = 4
) -> dict[str, Path]:
    """Fetch missing covers concurrently. Returns ``{cover id: base path}`` for successes."""
    wanted = list({cover_id(b.cover_url): b for b in books if cover_id(b.cover_url)}.values())
    if not wanted:
        return {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(wanted)))) as pool:
        paths = list(pool.map(lambda b: fetch_cover(cache_dir, b, download), wanted))
    return {
        str(cover_id(b.cover_url)): p for b, p in zip(wanted, paths, strict=True) if p is not None
    }
