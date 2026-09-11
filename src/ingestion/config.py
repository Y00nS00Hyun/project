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

#: Tokenizer used only to *count* tokens during chunking.
DEFAULT_TOKENIZER_NAME = "intfloat/multilingual-e5-small"

# ---------------------------------------------------------------------------
# Embedding defaults (Design Freeze v1)
# ---------------------------------------------------------------------------

#: Local model. No embedding SaaS, no inference API.
DEFAULT_EMBEDDING_MODEL = "intfloat/multilingual-e5-small"

#: Pinned so a silently updated upstream model cannot change vectors under a
#: corpus that was already embedded. Override only with a deliberate reindex.
DEFAULT_EMBEDDING_MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"

#: Must equal chunks.embedding VECTOR(384). Validated at construction.
REQUIRED_EMBEDDING_DIMENSION = 384

# ---------------------------------------------------------------------------
# Title-aware ranking
#
# The semantic route scores a document by its best *body* chunk alone, so a
# file named "사용자매뉴얼-윤수현" had no advantage whatsoever for the query
# "수현" -- and because e5 similarities sit in a 0.78-0.82 band, it lost to
# documents that merely embedded a hair closer. Measured: 제목 회귀 R@1 0.20.
#
# The fix is additive, not a gate: every document the semantic route finds is
# still returned, only reordered. A paraphrase query whose answer shares no
# words with its title scores 0 here and is unaffected.
#
# 0.075 is the smallest weight that fixed every title query, chosen by sweeping
# 0.0 - 1.0 on the 30-query benchmark plus five title and eight generic
# queries:
#
#     w      제목 R@1   paraphrase R@1   partial R@1
#     0.000    0.20         0.75            0.67
#     0.050    0.80         0.75            0.67
#     0.075    1.00         0.75            0.67      <- smallest that works
#     0.100    1.00         0.88            0.50      <- partial regresses
#
# At 0.10 a common word in a file name starts to overpower semantic relevance:
# "사용자 계정 관리" promoted 사용자매뉴얼 over the proposal request that
# actually specifies account management. Smaller is preferred for that reason,
# not for caution's sake.
DEFAULT_TITLE_BOOST_WEIGHT = 0.075

#: Added to a document's score when the query occurs verbatim in its body AND
#: that occurrence is rare across the candidate set. Both conditions, or the
#: boost does not fire at all.
#:
#: Why the body needs its own signal: the semantic route scores a document by
#: the cosine of its best chunk, and e5 places this corpus in a 0.78-0.85 band.
#: A rare token living only in the body -- "Zookeeper" -- has no route into the
#: score, so two documents that never mention it outranked one that mentions it
#: six times.
#:
#: 0.10 is comfortably above that observed band (~0.07 wide) so a body match
#: outranks documents with none, while staying the same order of magnitude as
#: the title boost. Measured insensitive from 0.02 to 1.00: once the boost
#: clears the band, more of it changes no ordering.
DEFAULT_BODY_EXACT_BOOST_WEIGHT = 0.10

#: The most of the candidate set an exact match may cover and still count as
#: evidence. Above it, the boost is suppressed entirely.
#:
#: Strength and discrimination are not the same thing, which is what the
#: evaluation measured: "파일 다운로드" occurs verbatim in 4 of 7 documents and
#: "Zookeeper" in 2. Both are exact matches; only one says anything about which
#: document to read. Boosting on the common phrase demoted the right answers.
#:
#: PROVISIONAL. Measured on a seven-document corpus, where 0.5 means "at most
#: three documents" -- technical terms land on 1-2 and common Korean phrases on
#: 4+, so the gap is wide and the value sits in the middle of it. That gap is a
#: property of this corpus, not of the rule. On a shared folder of 500
#: documents a term in 70 of them scores 0.14 and would boost; whether that is
#: right has to be measured there rather than assumed here. Re-measure before
#: treating this number as settled.
DEFAULT_BODY_EXACT_SELECTIVITY_MAX = 0.50

#: pg_trgm word_similarity floor for the lexical route. Provisional value from
#: the Korean search PoC. One global setting: never varied per query and never
#: exposed through the API.
DEFAULT_TRIGRAM_THRESHOLD = 0.20

DEFAULT_EMBEDDING_PROVIDER = "local"
DEFAULT_EMBEDDING_DEVICE = "cpu"
DEFAULT_EMBEDDING_BATCH_SIZE = 8

#: Extensions the schema's documents.file_type CHECK accepts. Anything else is
#: not a document as far as this system is concerned and is not discovered.
DISCOVERABLE_EXTENSIONS = ("hwp", "hwpx", "docx", "pdf")

# ---------------------------------------------------------------------------
# Job recovery
#
# A worker that dies mid-job leaves its row in RUNNING. The claim queries take
# only PENDING rows, so without recovery that document is never processed again
# and nothing says so -- it simply never appears in search.
#
# 900s is chosen against measured durations on the verification corpus: PARSE
# ran 9.5-25.1s and EMBED 24.2-119.5s for a 9MB, 1720-chunk report. Fifteen
# minutes is roughly 7.5x the longest observed job, which leaves room for
# documents several times larger while still bounding how long a crashed job
# sits invisible.
#
# The timeout has to cover the *whole* job, not the time since last progress:
# there is no heartbeat column and adding one would change the frozen schema,
# so staleness is measured from started_at. Too short is worse than too long --
# requeuing a job that is merely slow lets a second worker write the same
# revision concurrently.
DEFAULT_JOB_STALE_SECONDS = 900

#: Attempts allowed before a job is given up on. Matches the schema default for
#: processing_jobs.max_attempts.
DEFAULT_JOB_MAX_ATTEMPTS = 3

#: How long a file may stay missing before its document is soft-deleted.
#: The functional spec requires a grace period but leaves the value open, so it
#: is configurable with a conservative default.
DEFAULT_MISSING_GRACE_SECONDS = 24 * 60 * 60

#: Who may read a document the moment it is ingested.
#:
#:   "none"              nobody. Permissions are granted afterwards, by hand.
#:   "all_active_users"  every approved account, via one public permission row.
#:
#: The code default is "none" because that is the safe direction to be wrong
#: in: a deployment that forgets the setting ends up with documents nobody can
#: read, which is noticed immediately and fixed in one command. The opposite
#: mistake publishes a corpus quietly.
#:
#: This installation runs "all_active_users" -- see .env.example. That is sound
#: only because of a precondition outside this code: confidential, HR and
#: payroll material is excluded at collection time rather than filtered at read
#: time. If that ever stops being true, this setting is the first thing to
#: change.
DEFAULT_DOCUMENT_ACCESS = "none"
DOCUMENT_ACCESS_MODES = ("none", "all_active_users")


@dataclass(frozen=True)
class IngestionConfig:
    """Resolved ingestion settings."""

    shared_root: Path

    chunk_target_tokens: int = DEFAULT_CHUNK_TARGET_TOKENS
    chunk_max_tokens: int = DEFAULT_CHUNK_MAX_TOKENS
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    chunking_version: str = DEFAULT_CHUNKING_VERSION
    tokenizer_name: str = DEFAULT_TOKENIZER_NAME

    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_model_revision: str | None = DEFAULT_EMBEDDING_MODEL_REVISION
    embedding_dimension: int = REQUIRED_EMBEDDING_DIMENSION
    embedding_provider: str = DEFAULT_EMBEDDING_PROVIDER
    embedding_device: str = DEFAULT_EMBEDDING_DEVICE
    embedding_batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE
    embedding_cache_dir: str | None = None
    embedding_allow_download: bool = False

    trigram_threshold: float = DEFAULT_TRIGRAM_THRESHOLD
    document_access: str = DEFAULT_DOCUMENT_ACCESS
    title_boost_weight: float = DEFAULT_TITLE_BOOST_WEIGHT
    body_exact_boost_weight: float = DEFAULT_BODY_EXACT_BOOST_WEIGHT
    body_exact_selectivity_max: float = DEFAULT_BODY_EXACT_SELECTIVITY_MAX

    follow_symlinks: bool = False
    missing_grace_seconds: int = DEFAULT_MISSING_GRACE_SECONDS
    job_stale_seconds: int = DEFAULT_JOB_STALE_SECONDS
    job_max_attempts: int = DEFAULT_JOB_MAX_ATTEMPTS
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
        if self.job_stale_seconds < 1:
            raise ConfigurationError("job_stale_seconds must be >= 1")
        if self.job_max_attempts < 1:
            raise ConfigurationError("job_max_attempts must be >= 1")
        if self.embedding_dimension != REQUIRED_EMBEDDING_DIMENSION:
            # The column is VECTOR(384). Letting configuration disagree with the
            # schema would fail per-row at write time, deep inside a batch,
            # instead of at startup.
            raise ConfigurationError(
                f"embedding_dimension must be {REQUIRED_EMBEDDING_DIMENSION} to match "
                f"chunks.embedding VECTOR({REQUIRED_EMBEDDING_DIMENSION}), "
                f"got {self.embedding_dimension}"
            )
        if self.embedding_batch_size < 1:
            raise ConfigurationError("embedding_batch_size must be >= 1")
        if not (0.0 < self.trigram_threshold <= 1.0):
            raise ConfigurationError("trigram_threshold must be in (0.0, 1.0]")
        if self.title_boost_weight < 0.0:
            raise ConfigurationError("title_boost_weight must be >= 0")
        if self.body_exact_boost_weight < 0.0:
            raise ConfigurationError("body_exact_boost_weight must be >= 0")
        if not 0.0 <= self.body_exact_selectivity_max <= 1.0:
            # It is a fraction of the candidate set. Outside [0, 1] it either
            # never fires or always does, and both are better expressed by
            # setting the weight to 0.
            raise ConfigurationError(
                "body_exact_selectivity_max must be between 0 and 1"
            )
        if self.document_access not in DOCUMENT_ACCESS_MODES:
            # An unrecognised value is refused rather than treated as one of
            # them: guessing here decides who can read the corpus.
            raise ConfigurationError(
                "DOCUMENT_DEFAULT_ACCESS must be one of "
                + ", ".join(DOCUMENT_ACCESS_MODES)
            )
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
        embedding_model=os.environ.get("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        embedding_model_revision=os.environ.get(
            "EMBEDDING_MODEL_REVISION", DEFAULT_EMBEDDING_MODEL_REVISION
        ) or None,
        embedding_dimension=_int_env("EMBEDDING_DIMENSION", REQUIRED_EMBEDDING_DIMENSION),
        embedding_device=os.environ.get("EMBEDDING_DEVICE", DEFAULT_EMBEDDING_DEVICE),
        embedding_batch_size=_int_env("EMBEDDING_BATCH_SIZE", DEFAULT_EMBEDDING_BATCH_SIZE),
        embedding_cache_dir=os.environ.get("EMBEDDING_CACHE_DIR") or None,
        embedding_allow_download=os.environ.get("EMBEDDING_ALLOW_DOWNLOAD", "").lower()
        in {"1", "true", "yes"},
        trigram_threshold=float(
            os.environ.get("TRIGRAM_THRESHOLD", DEFAULT_TRIGRAM_THRESHOLD)
        ),
        document_access=os.environ.get(
            "DOCUMENT_DEFAULT_ACCESS", DEFAULT_DOCUMENT_ACCESS
        ).strip().lower(),
        title_boost_weight=float(
            os.environ.get("TITLE_BOOST_WEIGHT", DEFAULT_TITLE_BOOST_WEIGHT)
        ),
        body_exact_boost_weight=float(
            os.environ.get("BODY_EXACT_BOOST_WEIGHT", DEFAULT_BODY_EXACT_BOOST_WEIGHT)
        ),
        body_exact_selectivity_max=float(
            os.environ.get(
                "BODY_EXACT_SELECTIVITY_MAX", DEFAULT_BODY_EXACT_SELECTIVITY_MAX
            )
        ),
        follow_symlinks=os.environ.get("SCAN_FOLLOW_SYMLINKS", "").lower() in {"1", "true", "yes"},
        missing_grace_seconds=_int_env("MISSING_GRACE_SECONDS", DEFAULT_MISSING_GRACE_SECONDS),
        job_stale_seconds=_int_env("PROCESSING_JOB_STALE_SECONDS", DEFAULT_JOB_STALE_SECONDS),
        job_max_attempts=_int_env("PROCESSING_JOB_MAX_ATTEMPTS", DEFAULT_JOB_MAX_ATTEMPTS),
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
