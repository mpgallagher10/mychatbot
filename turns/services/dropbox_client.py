"""
Dropbox access: list a walk folder and download its images.

Two source shapes are supported:
  * Account path        "/Turns/P-1/2026-07-16"                (convention/fallback)
  * Shared-link folder  "https://www.dropbox.com/scl/fo/.../...?rlkey=..."  (prod)

Turn records store a shared-link folder in `Photo_Folder_URL__c`. Shared links
use different API calls than paths (`files_list_folder(shared_link=...)` and
`sharing_get_shared_link_file(...)`), and the `st=` query token in a browser URL
is short-lived — we normalize it off and keep the stable `rlkey`.

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
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from django.conf import settings

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}


@dataclass
class DropboxEntry:
    path: str          # unique key for this photo within a run
    name: str
    size: int
    content_hash: str  # Dropbox's own content hash (not sha256)
    # Shared-link download context (empty for account-path entries).
    shared_url: str = ""
    rel_path: str = field(default="")  # path relative to the shared folder root


def _is_image(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(ext) for ext in _IMAGE_EXTENSIONS)


def is_shared_url(source: str) -> bool:
    s = (source or "").strip().lower()
    return s.startswith("http") and "dropbox.com" in s


def normalize_dropbox_shared_url(url: str) -> str:
    """
    Keep the stable shared-link identity (path + rlkey), drop volatile params
    (`st` browser token, `dl`). This makes the URL reusable across walks/refreshes
    and stable as a photo uniqueness key.
    """
    parsed = urlparse(url.strip())
    qs = parse_qs(parsed.query)
    keep = {k: v for k, v in qs.items() if k in {"rlkey"}}
    new_query = urlencode({k: v[0] for k, v in keep.items()})
    return urlunparse(parsed._replace(query=new_query, fragment=""))


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

    # --- Listing ------------------------------------------------------------
    def list_images(self, source: str) -> list[DropboxEntry]:
        """List image files under a source (account path or shared-link folder)."""
        if is_shared_url(source):
            return self._list_shared(normalize_dropbox_shared_url(source))
        return self._list_path(source)

    def _list_path(self, folder_path: str) -> list[DropboxEntry]:
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
        logger.info("dropbox: %s images under path %s", len(entries), folder_path)
        return entries

    def _list_shared(self, shared_url: str) -> list[DropboxEntry]:
        """
        Walk a shared-link folder. Shared-link listing does not support the
        `recursive` flag, so we descend subfolders manually. Entry `path_lower`
        values are relative to the shared root.
        """
        from dropbox.files import FileMetadata, FolderMetadata, SharedLink

        entries: list[DropboxEntry] = []
        pending = [""]  # relative paths still to visit ("" == shared root)
        while pending:
            rel = pending.pop()
            result = _retry(
                lambda: self._client.files_list_folder(
                    path=rel, shared_link=SharedLink(url=shared_url)
                )
            )
            while True:
                for entry in result.entries:
                    if isinstance(entry, FolderMetadata):
                        pending.append(entry.path_lower)
                    elif isinstance(entry, FileMetadata) and _is_image(entry.name):
                        entries.append(
                            DropboxEntry(
                                # Namespaced by the shared url so current/prior
                                # walks never collide on identical filenames.
                                path=f"{shared_url}#{entry.path_lower}",
                                name=entry.name,
                                size=entry.size,
                                content_hash=getattr(entry, "content_hash", "") or "",
                                shared_url=shared_url,
                                rel_path=entry.path_lower,
                            )
                        )
                if not result.has_more:
                    break
                result = _retry(
                    lambda: self._client.files_list_folder_continue(result.cursor)
                )
        logger.info("dropbox: %s images under shared link", len(entries))
        return entries

    # --- Download -----------------------------------------------------------
    def download(self, entry: DropboxEntry) -> bytes:
        """Download a single entry's bytes (path or shared-link mode)."""
        if entry.shared_url:
            _, response = _retry(
                lambda: self._client.sharing_get_shared_link_file(
                    url=entry.shared_url, path=entry.rel_path
                )
            )
        else:
            _, response = _retry(lambda: self._client.files_download(entry.path))
        return response.content

    def folder_exists(self, folder_path: str) -> bool:
        from dropbox.exceptions import ApiError

        try:
            _retry(lambda: self._client.files_get_metadata(folder_path))
            return True
        except ApiError:
            return False
