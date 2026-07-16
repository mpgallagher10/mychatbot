"""
Resolve where a walk's photos live and how to read them.

A photo source string is one of:
  * a Dropbox path            -> "/Turns/P-1/2026-07-16"      (convention/fallback)
  * a Dropbox folder URL      -> "https://www.dropbox.com/scl/fo/..."   (shared link)
  * a Google Drive folder URL -> "https://drive.google.com/drive/folders/..."

The turn record's `Photo_Folder_URL__c` is the real source in production; the
`/Turns/...` convention is the local/dev fallback. Path sources are supported
today; URL sources are gated until the exact `Photo_Folder_URL__c` format is
confirmed (a shared-link listing needs the real URL shape to implement safely).
"""
from __future__ import annotations

from enum import Enum


class SourceKind(str, Enum):
    DROPBOX_PATH = "dropbox_path"
    DROPBOX_URL = "dropbox_url"
    DRIVE_URL = "drive_url"
    UNKNOWN = "unknown"


class PhotoSourceUnsupported(Exception):
    """Raised for a source kind not yet wired for download."""


def classify_source(source: str) -> SourceKind:
    s = (source or "").strip()
    if not s:
        return SourceKind.UNKNOWN
    if s.startswith("/"):
        return SourceKind.DROPBOX_PATH
    lower = s.lower()
    if lower.startswith("http"):
        if "dropbox.com" in lower:
            return SourceKind.DROPBOX_URL
        if "drive.google.com" in lower or "docs.google.com" in lower:
            return SourceKind.DRIVE_URL
    return SourceKind.UNKNOWN
