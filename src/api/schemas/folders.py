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

    #: Every document the caller may browse, whether or not it sits in a
    #: folder. Not the sum of the depth-1 `document_count` values: a document
    #: at the top of the shared folder belongs to no folder and appears in no
    #: row, so summing would under-report -- and a shared folder with no
    #: subdirectories at all would report zero while holding documents.
    #:
    #: Lets a client draw a root row for the shared folder itself. The root has
    #: no path: selecting it means "no folder filter", which is the absence of
    #: the `folder_path` parameter rather than some value for it.
    total_documents: int = 0
