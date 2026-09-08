"""Ingestion-level failures.

These are distinct from :mod:`document_processing.parsers.exceptions`, which
describe what is wrong with a *document*. The errors here describe what is
wrong with a *file or its handling* -- before or around parsing.
"""

from __future__ import annotations


class IngestionError(Exception):
    """Base class for ingestion failures."""


class PathOutsideRootError(IngestionError):
    """A path resolved outside the configured shared root.

    Raised for traversal attempts and for symlinks escaping the root. Treated
    as a security event, never as a routine skip.
    """


class FileUnstableError(IngestionError):
    """The file changed while it was being read.

    The scan must not store a half-written file as a revision; the file is left
    for the next run.
    """


class FileVanishedError(IngestionError):
    """The file disappeared between discovery and reading."""


class ConfigurationError(IngestionError):
    """The ingestion configuration is unusable."""
