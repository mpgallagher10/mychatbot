"""
Dropbox access: list a walk folder and download its images.

The `dropbox` SDK is imported lazily so `manage.py check` and unit tests that
don't touch Dropbox never require the dependency or credentials.

Auth precedence:
  1. Refresh-token app auth (DROPBOX_APP_KEY/SECRET/REFRESH_TOKEN) — preferred,
     tokens never expire.
  2. Static short-lived access token (DROPBOX_ACCESS_TOKEN) — convenient for
     local testing.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from django.conf import settings

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}


@dataclass
class DropboxEntry:
    path: str          # full path_lower from Dropbox
    name: str
    size: int
    content_hash: str  # Dropbox's own content hash (not sha256)


def _is_image(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(ext) for ext in _IMAGE_EXTENSIONS)


def _retry(fn: Callable, *, attempts: int = 4, base_delay: float = 2.0):
    """Retry a Dropbox call with exponential backoff on transient errors."""
    from dropbox.exceptions import ApiError, InternalServerError, RateLimitError

    last_exc: Optional[Exception] = None
    for i in range(attempts):
        try:
            return fn()
        except (RateLimitError, InternalServerError) as exc:
            last_exc = exc
            delay = base_delay * (2 ** i)
            logger.warning("dropbox transient error, retrying in %ss: %s", delay, exc)
            time.sleep(delay)
        except ApiError:
            raise
    raise last_exc  # type: ignore[misc]


class DropboxClient:
    def __init__(self, client=None):
        self._client = client or self._build_client()

    @staticmethod
    def _build_client():
        if not (
            (settings.DROPBOX_REFRESH_TOKEN and settings.DROPBOX_APP_KEY)
            or settings.DROPBOX_ACCESS_TOKEN
        ):
            raise RuntimeError(
                "Dropbox not configured: set DROPBOX_REFRESH_TOKEN+DROPBOX_APP_KEY "
                "or DROPBOX_ACCESS_TOKEN"
            )
        import dropbox

        if settings.DROPBOX_REFRESH_TOKEN and settings.DROPBOX_APP_KEY:
            return dropbox.Dropbox(
                oauth2_refresh_token=settings.DROPBOX_REFRESH_TOKEN,
                app_key=settings.DROPBOX_APP_KEY,
                app_secret=settings.DROPBOX_APP_SECRET or None,
            )
        return dropbox.Dropbox(settings.DROPBOX_ACCESS_TOKEN)

    def list_images(self, folder_path: str) -> list[DropboxEntry]:
        """Recursively list image files under a folder, following pagination."""
        from dropbox.files import FileMetadata

        entries: list[DropboxEntry] = []
        result = _retry(
            lambda: self._client.files_list_folder(folder_path, recursive=True)
        )
        while True:
            for entry in result.entries:
                if isinstance(entry, FileMetadata) and _is_image(entry.name):
                    entries.append(
                        DropboxEntry(
                            path=entry.path_lower,
                            name=entry.name,
                            size=entry.size,
                            content_hash=getattr(entry, "content_hash", "") or "",
                        )
                    )
            if not result.has_more:
                break
            result = _retry(
                lambda: self._client.files_list_folder_continue(result.cursor)
            )
        logger.info("dropbox: %s images under %s", len(entries), folder_path)
        return entries

    def download(self, path: str) -> bytes:
        """Download a single file's bytes."""
        _, response = _retry(lambda: self._client.files_download(path))
        return response.content

    def iter_downloads(
        self, entries: list[DropboxEntry]
    ) -> Iterator[tuple[DropboxEntry, bytes]]:
        for entry in entries:
            yield entry, self.download(entry.path)

    def folder_exists(self, folder_path: str) -> bool:
        from dropbox.exceptions import ApiError

        try:
            _retry(lambda: self._client.files_get_metadata(folder_path))
            return True
        except ApiError:
            return False
