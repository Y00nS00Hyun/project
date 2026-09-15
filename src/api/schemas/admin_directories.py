"""Administrator directory listing and folder creation."""

from __future__ import annotations

from pydantic import BaseModel


class DirectoryOut(BaseModel):
    """A real directory under the shared folder, by canonical relative path.

    Never a server path. ``depth`` is 1 at the top level.
    """

    path: str
    name: str
    depth: int


class DirectoryListResponse(BaseModel):
    """Parents before children. The top level itself is not an entry."""

    directories: list[DirectoryOut]


class CreateDirectoryRequest(BaseModel):
    """``parent_path`` is a canonical path from the directory list; "" is the top."""

    model_config = {"extra": "forbid"}

    parent_path: str = ""
    name: str
