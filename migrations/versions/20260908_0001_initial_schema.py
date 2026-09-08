"""v1 initial schema (database-schema-v2.5)

Transcribes docs/database-schema-v2.5.md into executable DDL.

Scope note: this is the first migration, so it creates everything -- extensions,
16 application tables, composite FKs, indexes and one view. The authoritative
source is the schema document; this file must not drift from it
(tests/test_migrations.py::TestSchemaDrift enforces that).

Deliberately absent:
  * HNSW index on chunks.embedding -- initial search is exact cosine
    (Design Freeze v1). Adding it is a later, measured decision.
  * FTS GIN index on chunks.search_vector -- optional lexical extension only;
    the pg_trgm route does not use it.
  * Any DROP EXTENSION in downgrade -- see downgrade() docstring.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-08
"""
from __future__ import annotations

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Extensions
#
# Fail loudly if unavailable. There is no fallback: the schema genuinely needs
# vector (embeddings) and pg_trgm (lexical search). pgcrypto is declared by the
# schema document for gen_random_uuid(); see the note in the report -- on
# PostgreSQL 13+ that function is built in, so pgcrypto is carried for fidelity
# with the schema document rather than because a column depends on it.
# ---------------------------------------------------------------------------
EXTENSIONS = [
    """
    CREATE EXTENSION IF NOT EXISTS vector;
    """,
    """
    CREATE EXTENSION IF NOT EXISTS pgcrypto;
    """,
    """
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
    """,]


# ---------------------------------------------------------------------------
# Tables, in dependency order.
# ---------------------------------------------------------------------------
TABLES = [
    """
    CREATE TABLE departments (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

        name TEXT NOT NULL UNIQUE,

        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    """
    CREATE TABLE users (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

        sso_subject TEXT NOT NULL UNIQUE,

        name TEXT,
        email TEXT,

        department_id UUID
            REFERENCES departments(id),

        is_active BOOLEAN NOT NULL DEFAULT TRUE,

        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    """
    CREATE TABLE documents (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

        title TEXT NOT NULL,

        original_filename TEXT NOT NULL,

        source_path TEXT NOT NULL,

        file_type TEXT NOT NULL
            CHECK (
                file_type IN (
                    'hwp',
                    'hwpx',
                    'docx',
                    'pdf'
                )
            ),

        department_id UUID
            REFERENCES departments(id),

        owner_id UUID
            REFERENCES users(id),

        /*
         * latest_revision_id
         * 공유폴더에서 가장 최근 발견한 revision.
         * 처리 완료 여부와 무관하다.
         */
        latest_revision_id UUID,

        /*
         * current_revision_id
         * 검색/RAG에 노출할 가장 최근 READY revision.
         */
        current_revision_id UUID,

        /*
         * File Sync 상태
         */
        last_seen_at TIMESTAMPTZ,

        missing_since TIMESTAMPTZ,

        is_deleted BOOLEAN NOT NULL DEFAULT FALSE,

        deleted_at TIMESTAMPTZ,

        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

        CHECK (
            (is_deleted = FALSE)
            OR
            (is_deleted = TRUE AND deleted_at IS NOT NULL)
        )
    );
    """,
    """
    CREATE TABLE document_revisions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

        document_id UUID NOT NULL
            REFERENCES documents(id)
            ON DELETE CASCADE,

        revision_no INT NOT NULL
            CHECK (revision_no > 0),

        /*
         * 원본 정보
         */
        content_hash TEXT NOT NULL,

        file_size BIGINT
            CHECK (file_size IS NULL OR file_size >= 0),

        source_mtime TIMESTAMPTZ,

        /*
         * 이 revision을 처음 ingest했을 당시의 경로.
         * documents.source_path는 이후 이동 시 변경되므로
         * historical provenance를 위해 별도 보존.
         */
        source_path_at_ingest TEXT NOT NULL,

        /*
         * document_year  (v2.5 신규)
         *
         * 이 revision의 '내용이 의미하는' 문서 기준 연도.
         * 예: "2026년 사업계획서" -> 2026
         *
         * API Contract v1의 검색 필터 year=2026 은
         *   current READY revision.document_year = 2026
         * 을 의미한다.
         *
         * source_mtime(공유폴더 파일 수정 시각)이나 created_at(시스템 발견 시각)을
         * 문서 연도로 대용하지 않는다. 두 값은 문서 내용상의 연도와 다를 수 있다.
         *
         * 연도를 신뢰할 수 없으면 NULL. 추측값을 넣지 않는다.
         * 값을 채우는 추출 로직은 File Sync / Ingestion 단계에서 구현한다.
         */
        document_year SMALLINT
            CHECK (
                document_year IS NULL
                OR document_year BETWEEN 1900 AND 2100
            ),

        /*
         * Parser 결과
         *
         * extracted_text:
         * 당시 parsing 결과를 재현하기 위한 최소 text snapshot.
         *
         * parsed_structure:
         * Parser가 추출한 표/문단 및 원문 위치를 보존한다.
         */
        extracted_text TEXT,

        /*
         * parsed_structure
         * 표/문단 등의 구조화 결과. row/column/cell/span을 유지한다.
         * independent table-aware OFF여도 구조를 삭제하거나 text로 덮어쓰지 않는다.
         *
         * parser가 남긴 경고(예: 병합 셀 존재, 표 주소 판독 실패,
         * 스캔 문서 의심 등)도 별도 상태 컬럼을 만들지 않고 여기에 함께 저장한다.
         * 경고는 검색 가능 여부(is_ready)에 영향을 주지 않는다.
         */
        parsed_structure JSONB,

        /*
         * Processing 상태
         */
        /*
         * parse_status
         * Worker의 '실행 상태'만 나타낸다.
         *
         * SUCCESS = parser가 파일을 정상적으로 분석하고 결과를 반환함.
         * FAILED  = 예상하지 못한 parser/worker 실패.
         *
         * 주의: 스캔 문서(OCR_REQUIRED), 빈 문서(EMPTY_DOCUMENT),
         * 암호화 문서(ENCRYPTED)는 parser가 '정상적으로 판정한' 결과이므로
         * parse_status = 'SUCCESS'가 될 수 있다.
         * 결과의 의미는 parse_result_code로 표현한다.
         */
        parse_status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (
                parse_status IN (
                    'PENDING',
                    'RUNNING',
                    'SUCCESS',
                    'FAILED'
                )
            ),

        /*
         * parse_result_code
         * 파싱 '결과의 의미'.
         *
         * NULL = 아직 파싱 결과가 나오지 않은 상태.
         *
         * 검색 가능한 본문을 실제로 확보한 경우에만 TEXT_EXTRACTED다.
         * 원인을 특정할 수 없는 실패는 더 구체적인 코드로 분류하지 않고
         * PARSE_FAILED로 기록한다.
         */
        parse_result_code TEXT
            CHECK (
                parse_result_code IS NULL
                OR parse_result_code IN (
                    'TEXT_EXTRACTED',
                    'EMPTY_DOCUMENT',
                    'OCR_REQUIRED',
                    'ENCRYPTED',
                    'CORRUPT',
                    'UNSUPPORTED_FORMAT',
                    'PARSE_FAILED'
                )
            ),

        /*
         * downstream 처리 상태
         *
         * SKIPPED
         * 본문을 확보하지 못한 revision(OCR_REQUIRED, EMPTY_DOCUMENT,
         * ENCRYPTED 등)에 대해 실행하지 않기로 결정한 상태.
         * 실패(FAILED)와 구분한다.
         */
        embedding_status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (
                embedding_status IN (
                    'PENDING',
                    'RUNNING',
                    'SUCCESS',
                    'FAILED',
                    'SKIPPED'
                )
            ),

        summary_status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (
                summary_status IN (
                    'PENDING',
                    'RUNNING',
                    'SUCCESS',
                    'FAILED',
                    'SKIPPED'
                )
            ),

        tagging_status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (
                tagging_status IN (
                    'PENDING',
                    'RUNNING',
                    'SUCCESS',
                    'FAILED',
                    'SKIPPED'
                )
            ),

        /*
         * 검색 가능 여부.
         *
         * parse_status = 'SUCCESS'만으로는 부족하다.
         * 스캔/빈/암호화 문서도 parser는 정상 판정하므로 SUCCESS가 될 수 있고,
         * 이들은 검색 가능한 본문을 갖지 않는다.
         *
         * Summary/Tagging 성공 여부는 검색 READY와 무관하다.
         *
         * parse_result_code는 nullable이므로 COALESCE로 감싼다.
         * 감싸지 않으면 아직 파싱 전(NULL)인 revision의 is_ready가
         * FALSE가 아니라 NULL이 되어 NOT is_ready 같은 조건에서 누락된다.
         */
        is_ready BOOLEAN GENERATED ALWAYS AS (
            parse_status = 'SUCCESS'
            AND COALESCE(parse_result_code, '') = 'TEXT_EXTRACTED'
            AND embedding_status = 'SUCCESS'
        ) STORED,

        /*
         * Parser / Chunking provenance
         */
        parser_name TEXT,

        parser_version TEXT,

        chunking_version TEXT,

        /*
         * Embedding provenance
         */
        embedding_provider TEXT,

        embedding_model TEXT,

        embedding_dimension INT,

        embedding_version TEXT,

        embedded_at TIMESTAMPTZ,

        /*
         * Summary
         */
        summary TEXT,

        summary_provider TEXT,

        summary_model TEXT,

        summary_prompt_version TEXT,

        summarized_at TIMESTAMPTZ,

        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

        UNIQUE (
            document_id,
            revision_no
        ),

        /*
         * documents → revision 복합 FK에서 사용.
         */
        UNIQUE (
            document_id,
            id
        )
    );
    """,
    """
    CREATE TABLE chunks (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

        document_revision_id UUID NOT NULL
            REFERENCES document_revisions(id)
            ON DELETE CASCADE,

        chunk_index INT NOT NULL
            CHECK (chunk_index >= 0),

        text TEXT NOT NULL,

        /*
         * MVP implementation default: local intfloat/multilingual-e5-small / 384d.
         * 실제 사내 corpus의 최적 모델이라는 뜻은 아니다.
         * 모델 변경으로 dimension이 달라지면 재임베딩과 migration이 필요하다.
         */
        embedding VECTOR(384),

        /*
         * optional FTS 확장용으로 유지. 기본 pg_trgm 경로에서는 사용하지 않는다.
         * 기본 ingestion에서 생성 의무가 없으며 NULL을 허용한다.
         * simple FTS는 MVP primary ranking engine이 아니다.
         */
        search_vector TSVECTOR,

        /*
         * page_number
         *
         * HWP/HWPX에서는 확보되지 않는다.
         * 두 포맷은 페이지 번호를 파일에 저장하지 않으며(페이지는 렌더링 결과),
         * PoC에서 신뢰할 수 있는 page anchor를 얻지 못했다.
         *
         * 따라서 HWP/HWPX 경로에서는 일반적으로 NULL이다.
         * 컬럼은 PDF 등 다른 포맷에서 확보 가능할 수 있으므로 유지한다.
         *
         * 추정하여 채워 넣지 않는다.
         */
        page_number INT
            CHECK (
                page_number IS NULL
                OR page_number > 0
            ),

        paragraph_start INT
            CHECK (
                paragraph_start IS NULL
                OR paragraph_start >= 0
            ),

        paragraph_end INT
            CHECK (
                paragraph_end IS NULL
                OR paragraph_end >= 0
            ),

        /*
         * section_title
         *
         * optional enhancement이며 citation의 필수 요소가 아니다.
         * 문서가 명시적인 개요/제목 스타일을 사용한 경우에만 저장한다.
         * heuristic이나 시각적 특성(글꼴 크기 등)으로 추정하여 만들어내지 않는다.
         */
        section_title TEXT,

        token_count INT
            CHECK (
                token_count IS NULL
                OR token_count >= 0
            ),

        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

        UNIQUE (
            document_revision_id,
            chunk_index
        ),

        CHECK (
            paragraph_start IS NULL
            OR paragraph_end IS NULL
            OR paragraph_end >= paragraph_start
        )
    );
    """,
    """
    CREATE TABLE document_permissions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

        document_id UUID NOT NULL
            REFERENCES documents(id)
            ON DELETE CASCADE,

        /*
         * 둘 중 정확히 하나만 설정.
         */
        user_id UUID
            REFERENCES users(id)
            ON DELETE CASCADE,

        department_id UUID
            REFERENCES departments(id)
            ON DELETE CASCADE,

        permission TEXT NOT NULL
            CHECK (
                permission IN (
                    'READ',
                    'WRITE',
                    'ADMIN'
                )
            ),

        granted_by UUID
            REFERENCES users(id),

        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

        CHECK (
            num_nonnulls(
                user_id,
                department_id
            ) = 1
        )
    );
    """,
    """
    CREATE TABLE processing_jobs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

        document_revision_id UUID NOT NULL
            REFERENCES document_revisions(id)
            ON DELETE CASCADE,

        job_type TEXT NOT NULL
            CHECK (
                job_type IN (
                    'PARSE',
                    'EMBED',
                    'SUMMARIZE',
                    'TAG',
                    'REINDEX'
                )
            ),

        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (
                status IN (
                    'PENDING',
                    'RUNNING',
                    'SUCCESS',
                    'FAILED'
                )
            ),

        attempt_count INT NOT NULL DEFAULT 0
            CHECK (attempt_count >= 0),

        max_attempts INT NOT NULL DEFAULT 3
            CHECK (max_attempts > 0),

        /*
         * Retry backoff 적용 후 다음 실행 가능 시각.
         */
        next_attempt_at TIMESTAMPTZ,

        /*
         * result_code
         * 시스템이 이해하는 구조화된 결과.
         *
         * 용도:
         *   - 운영 통계 / 실패 유형 집계 / Dashboard
         *   - Retry 판단
         *     (예: ENCRYPTED, UNSUPPORTED_FORMAT은 재시도해도 결과가 같다)
         *
         * PARSE job에서는 document_revisions.parse_result_code와 동일한
         * 값 도메인을 사용한다.
         * EMBED / SUMMARIZE / TAG / REINDEX job에서는 해당 작업의 결과 코드를
         * 사용하며, 값 도메인은 worker 구현 단계에서 확장한다.
         */
        result_code TEXT,

        /*
         * error_message
         * 사람이 원인을 확인하기 위한 상세 설명.
         * result_code를 대체하지 않는다.
         *
         * 예:
         *   result_code   = 'CORRUPT'
         *   error_message = 'File header is valid but BodyText stream
         *                    cannot be decoded...'
         */
        error_message TEXT,

        started_at TIMESTAMPTZ,

        finished_at TIMESTAMPTZ,

        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

        CHECK (
            attempt_count <= max_attempts
        )
    );
    """,
    """
    CREATE TABLE tags (
        id SERIAL PRIMARY KEY,

        name TEXT NOT NULL UNIQUE,

        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    """
    CREATE TABLE document_tags (
        document_id UUID NOT NULL
            REFERENCES documents(id)
            ON DELETE CASCADE,

        tag_id INT NOT NULL
            REFERENCES tags(id)
            ON DELETE CASCADE,

        source TEXT NOT NULL
            CHECK (
                source IN (
                    'MANUAL',
                    'SYSTEM'
                )
            ),

        created_by UUID
            REFERENCES users(id),

        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

        PRIMARY KEY (
            document_id,
            tag_id,
            source
        ),

        CHECK (
            source <> 'MANUAL'
            OR created_by IS NOT NULL
        )
    );
    """,
    """
    CREATE TABLE revision_tags (
        document_revision_id UUID NOT NULL
            REFERENCES document_revisions(id)
            ON DELETE CASCADE,

        tag_id INT NOT NULL
            REFERENCES tags(id)
            ON DELETE CASCADE,

        source TEXT NOT NULL DEFAULT 'AUTO'
            CHECK (
                source IN (
                    'AUTO',
                    'SYSTEM'
                )
            ),

        confidence DOUBLE PRECISION
            CHECK (
                confidence IS NULL
                OR (
                    confidence >= 0
                    AND confidence <= 1
                )
            ),

        llm_provider TEXT,

        llm_model TEXT,

        prompt_version TEXT,

        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

        PRIMARY KEY (
            document_revision_id,
            tag_id,
            source
        )
    );
    """,
    """
    CREATE TABLE favorites (
        user_id UUID NOT NULL
            REFERENCES users(id)
            ON DELETE CASCADE,

        document_id UUID NOT NULL
            REFERENCES documents(id)
            ON DELETE CASCADE,

        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

        PRIMARY KEY (
            user_id,
            document_id
        )
    );
    """,
    """
    CREATE TABLE recent_views (
        user_id UUID NOT NULL
            REFERENCES users(id)
            ON DELETE CASCADE,

        document_id UUID NOT NULL
            REFERENCES documents(id)
            ON DELETE CASCADE,

        last_viewed_at TIMESTAMPTZ NOT NULL DEFAULT now(),

        view_count INT NOT NULL DEFAULT 1
            CHECK (view_count > 0),

        PRIMARY KEY (
            user_id,
            document_id
        )
    );
    """,
    """
    CREATE TABLE chat_sessions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

        user_id UUID NOT NULL
            REFERENCES users(id),

        title TEXT,

        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    """
    CREATE TABLE chat_messages (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

        session_id UUID NOT NULL
            REFERENCES chat_sessions(id)
            ON DELETE CASCADE,

        role TEXT NOT NULL
            CHECK (
                role IN (
                    'user',
                    'assistant',
                    'system'
                )
            ),

        content TEXT NOT NULL,

        /*
         * refused  (v2.5 신규)
         *
         * assistant 답변이 '근거 부족으로 거절'인지 여부.
         *
         * API Contract v1은 refused 를 독립 boolean으로 반환하며,
         * answer 문자열 파싱이나 sources 개수로 추론하는 것을 금지한다.
         * 근거가 있으면서도 거절하는 경우가 성립하므로 sources 로 유도할 수 없고,
         * 저장하지 않으면 과거 세션 재조회 시 값을 재현할 수 없다.
         * 기능명세 §23.2의 Refusal Correctness 지표도 이 컬럼을 사용한다.
         *
         * role 별 규칙:
         *   assistant        -> TRUE 또는 FALSE (NOT NULL)
         *   user / system    -> NULL
         *
         * 완성된 assistant message 저장을 전제로 한다.
         * 스트리밍 중간 placeholder는 v1 범위 밖이다(API Contract OI-6).
         */
        refused BOOLEAN,

        /*
         * AI 호출 provenance
         */
        llm_provider TEXT,

        model TEXT,

        prompt_version TEXT,

        /*
         * 검색 파이프라인 재현을 위한 버전
         */
        retrieval_pipeline_version TEXT,

        input_tokens INT
            CHECK (
                input_tokens IS NULL
                OR input_tokens >= 0
            ),

        output_tokens INT
            CHECK (
                output_tokens IS NULL
                OR output_tokens >= 0
            ),

        latency_ms INT
            CHECK (
                latency_ms IS NULL
                OR latency_ms >= 0
            ),

        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

        /*
         * refused 는 assistant 메시지에만 존재한다. (v2.5 신규)
         * role domain 은 v2.4 그대로이며 새 role 을 추가하지 않았다.
         */
        CONSTRAINT ck_chat_messages_refused_role CHECK (
            (role = 'assistant' AND refused IS NOT NULL)
            OR
            (role <> 'assistant' AND refused IS NULL)
        )
    );
    """,
    """
    CREATE TABLE chat_message_sources (
        message_id UUID NOT NULL
            REFERENCES chat_messages(id)
            ON DELETE CASCADE,

        /*
         * 과거 답변 provenance 보존을 위해
         * 출처로 사용된 chunk는 함부로 삭제할 수 없음.
         */
        chunk_id UUID NOT NULL
            REFERENCES chunks(id)
            ON DELETE RESTRICT,

        source_label TEXT,

        retrieval_rank INT
            CHECK (
                retrieval_rank IS NULL
                OR retrieval_rank > 0
            ),

        /* RRF 기본 OFF일 때 NULL. vector cosine을 rrf_score로 기록하지 않는다. */
        rrf_score DOUBLE PRECISION,

        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

        PRIMARY KEY (
            message_id,
            chunk_id
        )
    );
    """,
    """
    CREATE TABLE audit_logs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

        actor_user_id UUID
            REFERENCES users(id),

        action TEXT NOT NULL,

        target_type TEXT,

        target_id UUID,

        /*
         * 민감하지 않은 추가 정보만 저장.
         * Query/문서본문/Prompt 전체를 무조건 넣지 않는다.
         */
        metadata JSONB,

        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,]


# ---------------------------------------------------------------------------
# Composite foreign keys.
#
# documents.current/latest_revision_id must point at a revision *of that same
# document*. A plain FK to document_revisions(id) would allow document A to
# point at document B's revision. These are added after both tables exist
# because the reference is circular, and are DEFERRABLE INITIALLY DEFERRED so a
# document and its first revision can be inserted in one transaction.
# ---------------------------------------------------------------------------
COMPOSITE_FKS = [
    """
    ALTER TABLE documents
    ADD CONSTRAINT fk_documents_latest_revision
    FOREIGN KEY (
        id,
        latest_revision_id
    )
    REFERENCES document_revisions(
        document_id,
        id
    )
    DEFERRABLE INITIALLY DEFERRED;
    """,
    """
    ALTER TABLE documents
    ADD CONSTRAINT fk_documents_current_revision
    FOREIGN KEY (
        id,
        current_revision_id
    )
    REFERENCES document_revisions(
        document_id,
        id
    )
    DEFERRABLE INITIALLY DEFERRED;
    """,]


# ---------------------------------------------------------------------------
# Indexes.
# ---------------------------------------------------------------------------
INDEXES = [
    """
    CREATE INDEX idx_users_department
        ON users(department_id);
    """,
    """
    CREATE UNIQUE INDEX uq_documents_active_source_path
        ON documents(source_path)
        WHERE is_deleted = FALSE;
    """,
    """
    CREATE INDEX idx_documents_department
        ON documents(department_id);
    """,
    """
    CREATE INDEX idx_documents_owner
        ON documents(owner_id);
    """,
    """
    CREATE INDEX idx_documents_last_seen
        ON documents(last_seen_at);
    """,
    """
    CREATE INDEX idx_documents_missing
        ON documents(missing_since)
        WHERE missing_since IS NOT NULL;
    """,
    """
    CREATE INDEX idx_revisions_content_hash
        ON document_revisions(content_hash);
    """,
    """
    CREATE INDEX idx_revisions_document
        ON document_revisions(document_id);
    """,
    """
    CREATE INDEX idx_revisions_ready
        ON document_revisions(document_id, revision_no DESC)
        WHERE is_ready = TRUE;
    """,
    """
    CREATE INDEX idx_revisions_created
        ON document_revisions(created_at);
    """,
    """
    CREATE INDEX idx_revisions_parse_result
        ON document_revisions(parse_result_code)
        WHERE parse_result_code IS NOT NULL;
    """,
    """
    CREATE INDEX idx_chunks_revision
        ON chunks(document_revision_id);
    """,
    """
    CREATE UNIQUE INDEX uq_permissions_document_user
        ON document_permissions(
            document_id,
            user_id
        )
        WHERE user_id IS NOT NULL;
    """,
    """
    CREATE UNIQUE INDEX uq_permissions_document_department
        ON document_permissions(
            document_id,
            department_id
        )
        WHERE department_id IS NOT NULL;
    """,
    """
    CREATE INDEX idx_permissions_user
        ON document_permissions(user_id, document_id)
        WHERE user_id IS NOT NULL;
    """,
    """
    CREATE INDEX idx_permissions_department
        ON document_permissions(department_id, document_id)
        WHERE department_id IS NOT NULL;
    """,
    """
    CREATE INDEX idx_jobs_queue
        ON processing_jobs(
            status,
            next_attempt_at,
            created_at
        );
    """,
    """
    CREATE INDEX idx_jobs_revision
        ON processing_jobs(
            document_revision_id,
            job_type
        );
    """,
    """
    CREATE INDEX idx_jobs_result_code
        ON processing_jobs(
            job_type,
            result_code
        )
        WHERE result_code IS NOT NULL;
    """,
    """
    CREATE UNIQUE INDEX uq_jobs_active
        ON processing_jobs(
            document_revision_id,
            job_type
        )
        WHERE status IN (
            'PENDING',
            'RUNNING'
        );
    """,
    """
    CREATE INDEX idx_chat_sessions_user
        ON chat_sessions(
            user_id,
            updated_at DESC
        );
    """,
    """
    CREATE INDEX idx_chat_messages_session
        ON chat_messages(
            session_id,
            created_at
        );
    """,
    """
    CREATE UNIQUE INDEX uq_chat_source_label
        ON chat_message_sources(
            message_id,
            source_label
        )
        WHERE source_label IS NOT NULL;
    """,
    """
    CREATE INDEX idx_audit_actor_created
        ON audit_logs(
            actor_user_id,
            created_at DESC
        );
    """,
    """
    CREATE INDEX idx_audit_target
        ON audit_logs(
            target_type,
            target_id,
            created_at DESC
        );
    """,
    """
    CREATE INDEX idx_documents_title_trgm
        ON documents
        USING GIN (title gin_trgm_ops);
    """,
    """
    CREATE INDEX idx_revisions_extracted_text_trgm
        ON document_revisions
        USING GIN (extracted_text gin_trgm_ops);
    """,]


# ---------------------------------------------------------------------------
# Views.
# ---------------------------------------------------------------------------
VIEWS = [
    """
    CREATE VIEW current_ready_chunks AS
    SELECT
        c.id AS chunk_id,
        c.document_revision_id,
        c.chunk_index,
        c.text,
        c.embedding,
        c.search_vector,
        c.page_number,
        c.paragraph_start,
        c.paragraph_end,
        c.section_title,
        c.token_count,

        r.document_id,
        r.revision_no,
        r.content_hash,

        d.title,
        d.original_filename,
        d.source_path,
        d.file_type,
        d.department_id,
        d.owner_id

    FROM chunks c

    JOIN document_revisions r
        ON r.id = c.document_revision_id

    JOIN documents d
        ON d.id = r.document_id

    WHERE
        d.is_deleted = FALSE
        AND d.current_revision_id = r.id
        AND r.is_ready = TRUE;
    """,]


TABLE_NAMES = [
    "departments",
    "users",
    "documents",
    "document_revisions",
    "chunks",
    "document_permissions",
    "processing_jobs",
    "tags",
    "document_tags",
    "revision_tags",
    "favorites",
    "recent_views",
    "chat_sessions",
    "chat_messages",
    "chat_message_sources",
    "audit_logs"
]

VIEW_NAMES = [
    "current_ready_chunks"
]

FK_NAMES = [
    "fk_documents_latest_revision",
    "fk_documents_current_revision"
]


def upgrade() -> None:
    """extensions -> tables -> composite FKs -> indexes -> views."""
    for statement in EXTENSIONS:
        op.execute(statement)
    for statement in TABLES:
        op.execute(statement)
    for statement in COMPOSITE_FKS:
        op.execute(statement)
    for statement in INDEXES:
        op.execute(statement)
    for statement in VIEWS:
        op.execute(statement)


def downgrade() -> None:
    """Reverse order: views -> composite FKs -> tables.

    Indexes are not dropped explicitly: every index here belongs to a table
    dropped below, and DROP TABLE removes them.

    The composite FKs are dropped before the tables because
    documents <-> document_revisions reference each other, and PostgreSQL
    cannot drop either side while the cycle stands.

    Extensions are intentionally NOT dropped. They are database-wide objects
    that other schemas may rely on, and dropping `vector` would silently
    invalidate any other table using the type. Removing an extension is a
    separate, deliberate operational decision.
    """
    for view in reversed(VIEW_NAMES):
        op.execute(f"DROP VIEW IF EXISTS {view}")
    for name in reversed(FK_NAMES):
        op.execute(f"ALTER TABLE documents DROP CONSTRAINT IF EXISTS {name}")
    for table in reversed(TABLE_NAMES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
