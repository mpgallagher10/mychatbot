"""
Dropbox folder convention: /Turns/{property_id}/{yyyy-mm-dd}/

Enforcing this convention is the cheapest reliability win in the pipeline: it
makes "which photos belong to this walk?" and "where is the prior walk?" pure
string operations instead of guesswork.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

FOLDER_ROOT = "/Turns"

# /Turns/<property_id>/<yyyy-mm-dd>  (trailing slash optional)
_PATH_RE = re.compile(
    r"^/Turns/(?P<property_id>[^/]+)/(?P<walk_date>\d{4}-\d{2}-\d{2})/?$"
)


@dataclass(frozen=True)
class FolderRef:
    property_id: str
    walk_date: date
    path: str

    @property
    def normalized_path(self) -> str:
        return f"{FOLDER_ROOT}/{self.property_id}/{self.walk_date.isoformat()}"


class FolderConventionError(ValueError):
    """Raised when a path does not match /Turns/{property_id}/{yyyy-mm-dd}/."""


def build_folder_path(property_id: str, walk_date: date) -> str:
    return f"{FOLDER_ROOT}/{property_id}/{walk_date.isoformat()}"


def parse_folder_path(path: str) -> FolderRef:
    """Parse and validate a Dropbox folder path against the convention."""
    if not path:
        raise FolderConventionError("empty folder path")
    cleaned = "/" + path.strip().strip("/")
    m = _PATH_RE.match(cleaned + "/")  # tolerate missing trailing slash
    if not m:
        raise FolderConventionError(
            f"path {path!r} does not match /Turns/{{property_id}}/{{yyyy-mm-dd}}/"
        )
    try:
        walk = date.fromisoformat(m.group("walk_date"))
    except ValueError as exc:  # pragma: no cover - regex already constrains shape
        raise FolderConventionError(str(exc)) from exc
    return FolderRef(
        property_id=m.group("property_id"),
        walk_date=walk,
        path=cleaned.rstrip("/"),
    )
