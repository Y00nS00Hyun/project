"""Response for GET /api/v1/search/years."""

from __future__ import annotations

from pydantic import BaseModel


class YearListResponse(BaseModel):
    """Years that at least one document the caller may search carries.

    Newest first. No counts and no "unknown year" entry: this populates the
    year filter, and a filter offering a year with nothing in it -- or a year
    only an unreadable document has -- is what it replaces.
    """

    years: list[int]
