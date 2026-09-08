"""HTTP layer for the Search / Document API (API Contract v1)."""

from __future__ import annotations

from .app import API_PREFIX, create_app
from .errors import ERROR_CODES, ApiError

__all__ = ["API_PREFIX", "ERROR_CODES", "ApiError", "create_app"]
