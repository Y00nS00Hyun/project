"""File Sync + Ingestion foundation.

Pipeline implemented here:

    shared folder -> discovery -> documents -> document_revisions
                  -> PARSE job -> parser -> chunks

Embedding, current-revision promotion, search and the API are later stages and
are deliberately absent. See docs/ingestion-foundation.md.
"""

from __future__ import annotations

from .chunker import Chunk, chunk_document
from .config import IngestionConfig, config_from_env, database_url
from .document_year import extract_document_year, year_for_file
from .exceptions import (
    ConfigurationError,
    FileUnstableError,
    FileVanishedError,
    IngestionError,
    PathOutsideRootError,
)
from .file_identity import FileFingerprint, fingerprint
from .file_scanner import DiscoveredFile, resolve_source_path, scan_files
from .ingestion_service import IngestionResult, IngestionService, default_connection_factory
from .repository import IngestionRepository
from .sync_service import ScanResult, SyncService
from .tokenizers import HuggingFaceTokenizer, SimpleTokenizer, Tokenizer

__all__ = [
    "Chunk",
    "ConfigurationError",
    "DiscoveredFile",
    "FileFingerprint",
    "FileUnstableError",
    "FileVanishedError",
    "HuggingFaceTokenizer",
    "IngestionConfig",
    "IngestionError",
    "IngestionRepository",
    "IngestionResult",
    "IngestionService",
    "PathOutsideRootError",
    "ScanResult",
    "SimpleTokenizer",
    "SyncService",
    "Tokenizer",
    "chunk_document",
    "config_from_env",
    "database_url",
    "default_connection_factory",
    "extract_document_year",
    "fingerprint",
    "resolve_source_path",
    "scan_files",
    "year_for_file",
]
