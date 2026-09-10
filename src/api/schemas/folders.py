"""Folder navigation schema (API Contract v1.1, section 12)."""

from __future__ import annotations

from pydantic import BaseModel


class FolderOut(BaseModel):
    """One folder in the shared folder's visible structure.

    `path` and `name` are separate on purpose. `path` is the identity -- the
    canonical, reversible relative path -- and is what a client sends back as
    `folder_path`. `name` is the last segment rendered for reading, which for a
    folder created outside UTF-8 differs from the path entirely.

    A client must never rebuild a path by joining names. Send back the `path`
    this response gave you.

    Not present, deliberately: any server absolute path. Every value here is
    relative to the shared root, which the client never learns the location of.
    """

    path: str
    name: str
    parent_path: str | None = None
    depth: int
    #: Documents anywhere in this folder's subtree that the caller may read.
    document_count: int


class FolderListResponse(BaseModel):
    """A flat list; the client assembles the tree from `parent_path`.

    Flat rather than nested so a large tree can be rendered incrementally and
    a single folder's row can be refreshed without re-fetching its subtree.

    No pagination: the tree is navigation, and a partial tree is not navigable.
    """

    items: list[FolderOut]
