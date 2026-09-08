"""Ingestion configuration.

Everything that could differ between environments is injected, never hardcoded:
the shared-folder root above all, but also the chunking parameters, which are a
*provisional* default from the Chunking + Embedding PoC and not a measured
optimum for real internal documents.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .exceptions import ConfigurationError

# ---------------------------------------------------------------------------
# Chunking defaults (Design Freeze v1)
#
# 64 tokens comes from a short synthetic corpus. It is an IMPLEMENTATION
# DEFAULT, not a validated production optimum -- see
# docs/poc/chunking-embedding-poc-report.md. It is configurable precisely so
# that re-measuring on the internal corpus can change it without code edits.
# ---------------------------------------------------------------------------
DEFAULT_CHUNK_TARGET_TOKENS = 64
DEFAULT_CHUNK_MAX_TOKENS = 64
DEFAULT_CHUNK_OVERLAP = 0

#: Recorded on every revision so chunks produced by different rules stay
#: distinguishable. Bump this whenever chunking behaviour changes.
DEFAULT_CHUNKING_VERSION = "paragraph-v1-64t-o0"

#: Tokenizer used only to *count* tokens. No model weights are loaded during
#: ingestion (Design Freeze: embedding is a later stage).
DEFAULT_TOKENIZER_NAME = "intfloat/multilingual-e5-small"

#: Extensions the schema's documents.file_type CHECK accepts. Anything else is
#: not a document as far as this system is concerned and is not discovered.
DISCOVERABLE_EXTENSIONS = ("hwp", "hwpx", "docx", "pdf")

#: How long a file may stay missing before its document is soft-deleted.
#: The functional spec requires a grace period but leaves the value open, so it
#: is configurable with a conservative default.
DEFAULT_MISSING_GRACE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class IngestionConfig:
    """Resolved ingestion settings."""

    shared_root: Path

    chunk_target_tokens: int = DEFAULT_CHUNK_TARGET_TOKENS
    chunk_max_tokens: int = DEFAULT_CHUNK_MAX_TOKENS
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    chunking_version: str = DEFAULT_CHUNKING_VERSION
    tokenizer_name: str = DEFAULT_TOKENIZER_NAME

    follow_symlinks: bool = False
    missing_grace_seconds: int = DEFAULT_MISSING_GRACE_SECONDS
    discoverable_extensions: tuple[str, ...] = field(default=DISCOVERABLE_EXTENSIONS)

    def __post_init__(self) -> None:
        if self.chunk_max_tokens < 1:
            raise ConfigurationError("chunk_max_tokens must be >= 1")
        if self.chunk_target_tokens < 1:
            raise ConfigurationError("chunk_target_tokens must be >= 1")
        if self.chunk_target_tokens > self.chunk_max_tokens:
            raise ConfigurationError("chunk_target_tokens must not exceed chunk_max_tokens")
        if self.chunk_overlap < 0:
            raise ConfigurationError("chunk_overlap must be >= 0")
        if self.chunk_overlap >= self.chunk_max_tokens:
            raise ConfigurationError("chunk_overlap must be smaller than chunk_max_tokens")
        if self.missing_grace_seconds < 0:
            raise ConfigurationError("missing_grace_seconds must be >= 0")
        if self.follow_symlinks:
            # Allowed, but the caller is opting out of the escape protection
            # that keeps a scan inside the shared root.
            pass

    @property
    def resolved_root(self) -> Path:
        return self.shared_root.resolve()


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc


def config_from_env(shared_root: Path | str | None = None) -> IngestionConfig:
    """Build configuration from the environment.

    ``SHARED_ROOT`` has no default: pointing a scanner at the wrong directory
    by accident is worse than refusing to start.
    """
    root = shared_root if shared_root is not None else os.environ.get("SHARED_ROOT")
    if not root:
        raise ConfigurationError(
            "SHARED_ROOT is not set. Point it at the shared folder to scan, "
            "e.g. SHARED_ROOT=/mnt/shared"
        )
    path = Path(root)
    if not path.is_dir():
        raise ConfigurationError(f"SHARED_ROOT is not a directory: {path}")

    return IngestionConfig(
        shared_root=path,
        chunk_target_tokens=_int_env("CHUNK_TARGET_TOKENS", DEFAULT_CHUNK_TARGET_TOKENS),
        chunk_max_tokens=_int_env("CHUNK_MAX_TOKENS", DEFAULT_CHUNK_MAX_TOKENS),
        chunk_overlap=_int_env("CHUNK_OVERLAP", DEFAULT_CHUNK_OVERLAP),
        chunking_version=os.environ.get("CHUNKING_VERSION", DEFAULT_CHUNKING_VERSION),
        tokenizer_name=os.environ.get("EMBEDDING_TOKENIZER", DEFAULT_TOKENIZER_NAME),
        follow_symlinks=os.environ.get("SCAN_FOLLOW_SYMLINKS", "").lower() in {"1", "true", "yes"},
        missing_grace_seconds=_int_env("MISSING_GRACE_SECONDS", DEFAULT_MISSING_GRACE_SECONDS),
    )


def database_url() -> str:
    """Read DATABASE_URL, failing loudly rather than guessing."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ConfigurationError(
            "DATABASE_URL is not set, e.g. "
            "postgresql://user:pass@host:5432/dbname"
        )
    # Alembic uses the SQLAlchemy form; psycopg wants the plain one.
    return url.replace("postgresql+psycopg://", "postgresql://", 1)
