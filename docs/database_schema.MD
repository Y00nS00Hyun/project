# 사내 문서 관리 시스템 — DB 스키마 (v2.2)

> **v2.1 대비 주요 변경**
>
> 1. `content_hash` UNIQUE 제거 — 동일 파일 복사본을 duplicate로 정상 저장
> 2. `latest_revision_id`와 `current_revision_id` 분리
> 3. `current/latest revision`이 반드시 해당 document 소속 revision을 가리키도록 복합 FK 적용
> 4. ACL의 polymorphic `subject_id` 제거 → `user_id` / `department_id` FK 분리
> 5. `(document_revision_id, chunk_index)` UNIQUE 적용
> 6. `chat_message_sources`에서 중복 `document_revision_id` 제거
> 7. revision에 parser / embedding / summary provider 및 version 정보 추가
> 8. AUTO 태그는 revision, MANUAL 태그는 document에 귀속
> 9. `recent_views`를 사용자×문서 1행 UPSERT 구조로 변경
> 10. 주요 상태값·타입에 CHECK constraint 적용
> 11. `version_group_id`는 MVP 스키마에서 제거
> 12. `REINDEX`는 MVP에서 재임베딩/검색 인덱스 재생성으로 한정
> 13. 최신 READY revision 검색 실수를 줄이기 위한 `current_ready_chunks` VIEW 추가
> 14. 보고서 생성 테이블은 P3(MVP+2)에서 별도 확장

---

# 1. 설계 원칙

## 1.1 문서와 Revision 분리

```text
documents
    │
    ├── latest_revision_id
    └── current_revision_id
             │
             ▼
document_revisions
             │
             ▼
           chunks
```

`documents`는 논리적인 문서를 나타내고, 실제 특정 시점의 콘텐츠는 `document_revisions`에서 관리한다.

### `latest_revision_id`

공유폴더에서 가장 최근 발견된 revision.

아직 Parsing/Embedding 중일 수도 있다.

### `current_revision_id`

현재 일반 검색 및 RAG에 노출되는 **가장 최근 READY revision**.

예:

```text
revision 1
parse SUCCESS
embedding SUCCESS
→ READY

파일 수정 발생

revision 2
parse RUNNING
embedding PENDING

latest_revision_id  = revision 2
current_revision_id = revision 1

revision 2 처리 완료

latest_revision_id  = revision 2
current_revision_id = revision 2
```

따라서 신규 revision 처리 중에도 기존 정상 문서가 검색에서 사라지지 않는다.

---

# 2. 테이블 관계

```text
departments
   │
   ├── users
   │
   └── documents
          │
          ├── document_permissions
          ├── document_tags
          ├── favorites
          └── recent_views
          │
          ▼
   document_revisions
          │
          ├── chunks
          ├── revision_tags
          └── processing_jobs


chat_sessions
   │
   └── chat_messages
          │
          └── chat_message_sources
                    │
                    └── chunks
                           │
                           └── document_revisions


tags
 ├── document_tags
 └── revision_tags


audit_logs
```

---

# 3. Extension

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 한국어 lexical search PoC에서 pg_trgm을 채택하는 경우 활성화
-- CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

---

# 4. Departments

```sql
CREATE TABLE departments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    name TEXT NOT NULL UNIQUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

# 5. Users

사내 SSO의 사용자 identity와 시스템 내부 사용자 정보를 연결한다.

```sql
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

CREATE INDEX idx_users_department
    ON users(department_id);
```

### 원칙

권한 판정 시 프론트엔드에서 전달된 `user_id`를 신뢰하지 않는다.

SSO 인증 후 서버가 `sso_subject`를 기준으로 현재 사용자 identity를 결정한다.

---

# 6. Documents

`documents`는 실제 콘텐츠가 아니라 **논리적 문서 및 현재 파일 위치**를 나타낸다.

```sql
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
```

활성 문서의 현재 경로는 중복되지 않아야 한다.

```sql
CREATE UNIQUE INDEX uq_documents_active_source_path
    ON documents(source_path)
    WHERE is_deleted = FALSE;
```

```sql
CREATE INDEX idx_documents_department
    ON documents(department_id);

CREATE INDEX idx_documents_owner
    ON documents(owner_id);

CREATE INDEX idx_documents_last_seen
    ON documents(last_seen_at);

CREATE INDEX idx_documents_missing
    ON documents(missing_since)
    WHERE missing_since IS NOT NULL;
```

> `source_path`는 File Sync 단계에서 slash, 상대경로, 대소문자 정책 등을 통일한 canonical path로 저장한다.

---

# 7. Document Revisions

`document_revisions`는 특정 시점의 문서 콘텐츠를 나타낸다.

**콘텐츠와 출처 정보는 불변으로 취급하고, 처리 상태·요약 등 파생 metadata만 갱신한다.**

```sql
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
     * Parser 결과
     *
     * extracted_text:
     * 당시 parsing 결과를 재현하기 위한 최소 text snapshot.
     *
     * parsed_structure:
     * 표/문단 등의 구조화 결과가 필요할 경우 사용.
     */
    extracted_text TEXT,

    parsed_structure JSONB,

    /*
     * Processing 상태
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

    embedding_status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (
            embedding_status IN (
                'PENDING',
                'RUNNING',
                'SUCCESS',
                'FAILED'
            )
        ),

    summary_status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (
            summary_status IN (
                'PENDING',
                'RUNNING',
                'SUCCESS',
                'FAILED'
            )
        ),

    tagging_status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (
            tagging_status IN (
                'PENDING',
                'RUNNING',
                'SUCCESS',
                'FAILED'
            )
        ),

    /*
     * 검색 가능 여부.
     * Summary/Tagging 성공 여부는 검색 READY와 무관하다.
     */
    is_ready BOOLEAN GENERATED ALWAYS AS (
        parse_status = 'SUCCESS'
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
```

### Content Hash

동일 hash가 여러 revision/document에 존재하는 것은 정상적인 상황이다.

예:

* 같은 파일 복사본
* 서로 다른 공유폴더 위치
* 파일 이동 추론이 불확실한 경우

따라서 UNIQUE를 걸지 않는다.

```sql
CREATE INDEX idx_revisions_content_hash
    ON document_revisions(content_hash);
```

기타 인덱스:

```sql
CREATE INDEX idx_revisions_document
    ON document_revisions(document_id);

CREATE INDEX idx_revisions_ready
    ON document_revisions(document_id, revision_no DESC)
    WHERE is_ready = TRUE;

CREATE INDEX idx_revisions_created
    ON document_revisions(created_at);
```

---

# 8. Documents → Revision Pointer FK

단순히:

```text
current_revision_id → document_revisions.id
```

만 연결하면 document A가 document B의 revision을 참조할 수 있다.

따라서 document ID까지 포함한 복합 FK로 제한한다.

```sql
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
```

```sql
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
```

이를 통해:

```text
document A
→ document B의 revision
```

과 같은 잘못된 참조는 DB 차원에서 차단한다.

### 서비스 계층 규칙

DB FK는 revision 소속 관계를 보장한다.

다만:

```text
current_revision_id가 반드시 is_ready=true인가?
```

는 행 간 조건이므로 Application Service에서 transaction으로 보장한다.

`current_revision_id` 변경 조건:

```text
revision.parse_status = SUCCESS
AND
revision.embedding_status = SUCCESS
AND
document.is_deleted = FALSE
```

---

# 9. Chunks

RAG 및 검색의 최소 retrieval 단위다.

```sql
CREATE TABLE chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    document_revision_id UUID NOT NULL
        REFERENCES document_revisions(id)
        ON DELETE CASCADE,

    chunk_index INT NOT NULL
        CHECK (chunk_index >= 0),

    text TEXT NOT NULL,

    /*
     * 임베딩 모델 확정 후 실제 차원을 조정한다.
     *
     * 예시는 1536이며 최종 확정값이 아니다.
     */
    embedding VECTOR(1536),

    /*
     * PostgreSQL FTS 계열을 선택했을 경우 사용.
     * PGroonga 채택 시 사용 여부를 재검토.
     */
    search_vector TSVECTOR,

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
```

```sql
CREATE INDEX idx_chunks_revision
    ON chunks(document_revision_id);
```

PostgreSQL FTS 계열을 사용할 경우:

```sql
CREATE INDEX idx_chunks_fts
    ON chunks
    USING GIN(search_vector);
```

Embedding은 초기에는 exact search를 baseline으로 사용한다.

```sql
-- corpus 증가 후 검토
--
-- CREATE INDEX idx_chunks_embedding_hnsw
-- ON chunks
-- USING hnsw (embedding vector_cosine_ops);
```

---

# 10. Current Ready Chunks View

과거 revision을 실수로 일반 검색에 포함시키지 않도록 기본 검색용 VIEW를 제공한다.

```sql
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
```

이 VIEW 자체는 ACL을 적용하지 않는다.

실제 검색에서는 반드시 추가로:

```text
current_ready_chunks
+
현재 사용자의 document ACL
```

조건을 적용한다.

---

# 11. Document Permissions

ACL에서 USER와 DEPARTMENT를 하나의 generic UUID로 저장하지 않는다.

실제 FK로 무결성을 보장한다.

```sql
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
```

한 사용자/부서에 동일 문서 permission row는 하나만 둔다.

```sql
CREATE UNIQUE INDEX uq_permissions_document_user
    ON document_permissions(
        document_id,
        user_id
    )
    WHERE user_id IS NOT NULL;
```

```sql
CREATE UNIQUE INDEX uq_permissions_document_department
    ON document_permissions(
        document_id,
        department_id
    )
    WHERE department_id IS NOT NULL;
```

검색 최적화:

```sql
CREATE INDEX idx_permissions_user
    ON document_permissions(user_id, document_id)
    WHERE user_id IS NOT NULL;
```

```sql
CREATE INDEX idx_permissions_department
    ON document_permissions(department_id, document_id)
    WHERE department_id IS NOT NULL;
```

### Permission Level

MVP에서는 명시적인 `DENY` 규칙을 두지 않는다.

권한 우선순위:

```text
ADMIN > WRITE > READ
```

사용자 직접 권한과 부서 권한이 동시에 존재하면 가장 높은 권한을 effective permission으로 사용한다.

예:

```text
부서 권한 = READ
사용자 권한 = WRITE

→ effective permission = WRITE
```

---

# 12. Processing Jobs

문서 처리 작업의 Queue 및 실행 이력을 관리한다.

```sql
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

    error_message TEXT,

    started_at TIMESTAMPTZ,

    finished_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (
        attempt_count <= max_attempts
    )
);
```

```sql
CREATE INDEX idx_jobs_queue
    ON processing_jobs(
        status,
        next_attempt_at,
        created_at
    );
```

```sql
CREATE INDEX idx_jobs_revision
    ON processing_jobs(
        document_revision_id,
        job_type
    );
```

동일 revision에서 동일 job이 동시에 여러 개 실행되는 것을 막는다.

```sql
CREATE UNIQUE INDEX uq_jobs_active
    ON processing_jobs(
        document_revision_id,
        job_type
    )
    WHERE status IN (
        'PENDING',
        'RUNNING'
    );
```

---

# 13. REINDEX 정책

MVP에서 `REINDEX`의 의미는 다음으로 제한한다.

```text
Embedding 재생성
Lexical search_vector 재생성
Vector/Search index 재구축
```

다음 작업은 `REINDEX`에 포함하지 않는다.

```text
Parser 변경으로 본문 구조 변경
Chunking algorithm 변경
기존 chunk text 변경
기존 chunk 분할 방식 변경
```

기존 `chunk_id`는 과거 RAG 출처로 사용될 수 있기 때문에, 이미 참조된 chunk를 임의로 덮어쓰지 않는다.

Parser/Chunking 전략 자체를 변경하는 기능은 MVP 이후 별도의 재처리 정책으로 설계한다.

---

# 14. Tags

Tag 자체는 전역 dictionary로 관리한다.

```sql
CREATE TABLE tags (
    id SERIAL PRIMARY KEY,

    name TEXT NOT NULL UNIQUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

# 15. Document Tags

사용자가 직접 입력하거나 시스템이 문서 자체에 부여하는 장기적인 태그.

주로:

```text
MANUAL
SYSTEM
```

을 사용한다.

```sql
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
```

---

# 16. Revision Tags

LLM이 특정 콘텐츠 revision을 분석해서 생성한 AUTO 태그는 해당 revision에 귀속한다.

Revision 변경 시 과거 AUTO 태그가 최신 문서의 태그처럼 남는 문제를 방지한다.

```sql
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
```

일반 검색 UI에서 보여주는 자동 태그는 기본적으로:

```text
documents.current_revision_id
→ revision_tags
```

만 조회한다.

---

# 17. Favorites

```sql
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
```

현재 접근 권한이 사라진 문서는 favorites에 row가 남아 있어도 UI에서 노출하지 않는다.

---

# 18. Recent Views

`recent_views`는 감사 로그가 아니라 사용자의 최근 문서 목록을 빠르게 제공하기 위한 상태 테이블이다.

문서를 볼 때마다 새로운 row를 추가하지 않는다.

```sql
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
```

조회 시:

```sql
INSERT INTO recent_views (
    user_id,
    document_id,
    last_viewed_at,
    view_count
)
VALUES (
    $1,
    $2,
    now(),
    1
)

ON CONFLICT (
    user_id,
    document_id
)

DO UPDATE SET
    last_viewed_at = now(),
    view_count = recent_views.view_count + 1;
```

최근 조회 목록:

```sql
SELECT *
FROM recent_views
WHERE user_id = $1
ORDER BY last_viewed_at DESC
LIMIT 20;
```

전체 실제 조회 이력은 `audit_logs`에서 관리한다.

---

# 19. Chat Sessions

```sql
CREATE TABLE chat_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    user_id UUID NOT NULL
        REFERENCES users(id),

    title TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

```sql
CREATE INDEX idx_chat_sessions_user
    ON chat_sessions(
        user_id,
        updated_at DESC
    );
```

---

# 20. Chat Messages

```sql
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

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

```sql
CREATE INDEX idx_chat_messages_session
    ON chat_messages(
        session_id,
        created_at
    );
```

---

# 21. Chat Message Sources

`chunk_id` 자체가 revision을 가리키므로 `document_revision_id`를 중복 저장하지 않는다.

```text
chat_message_sources
→ chunk
→ document_revision
→ document
```

```sql
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

    rrf_score DOUBLE PRECISION,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (
        message_id,
        chunk_id
    )
);
```

```sql
CREATE UNIQUE INDEX uq_chat_source_label
    ON chat_message_sources(
        message_id,
        source_label
    )
    WHERE source_label IS NOT NULL;
```

과거 답변의 revision은 다음 관계로 조회한다.

```text
chat_message_sources.chunk_id
→ chunks.document_revision_id
→ document_revisions.id
```

따라서 source와 revision이 서로 다른 잘못된 데이터가 저장될 가능성을 제거한다.

---

# 22. 과거 Chat ACL 정책

과거 assistant 메시지의 source를 조회한 뒤:

```text
chunk
→ revision
→ document
→ 현재 document_permissions
```

을 다시 확인한다.

현재 사용자가 source document에 접근할 수 없다면 해당 답변은 정책에 따라 마스킹한다.

예:

```text
이 답변에 사용된 일부 문서에 대한 현재 접근 권한이 없습니다.
```

`chat_message_sources` 자체는 과거 provenance를 위해 삭제하지 않는다.

---

# 23. Audit Logs

실제 보안/감사를 위한 append-only 성격의 로그다.

```sql
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
```

```sql
CREATE INDEX idx_audit_actor_created
    ON audit_logs(
        actor_user_id,
        created_at DESC
    );
```

```sql
CREATE INDEX idx_audit_target
    ON audit_logs(
        target_type,
        target_id,
        created_at DESC
    );
```

대표 action:

```text
DOCUMENT_VIEW
DOCUMENT_DOWNLOAD

SEARCH

TAG_ADD
TAG_REMOVE

PERMISSION_CREATE
PERMISSION_UPDATE
PERMISSION_DELETE

RAG_QUERY

REPROCESS
```

보고서 자동 생성 기능이 추가되면:

```text
REPORT_GENERATE
REPORT_DOWNLOAD
```

등을 추가한다.

---

# 24. File Sync 처리 규칙

DB 상태 변화는 다음 원칙을 따른다.

## 24.1 신규 파일

```text
파일 발견
↓
documents INSERT
↓
document_revisions revision 1 INSERT
↓
documents.latest_revision_id = revision 1
↓
PARSE job 생성
```

처리 완료 전:

```text
current_revision_id = NULL
```

처리 완료 후:

```text
current_revision_id = revision 1
```

---

## 24.2 기존 파일 수정

기존:

```text
revision 1 = READY
current_revision_id = revision 1
```

파일 hash 변경:

```text
revision 2 생성
latest_revision_id = revision 2
current_revision_id = revision 1 유지
```

revision 2 처리 완료:

```text
current_revision_id = revision 2
```

---

## 24.3 파일 이동

다음 조건에서만 자동 이동으로 판단한다.

```text
기존 경로 소멸
+
신규 경로 등장
+
content_hash 동일
+
1:1 대응이 명확
```

처리:

```text
documents.source_path 갱신
```

새 revision은 만들지 않는다.

---

## 24.4 Duplicate

동일 hash가 여러 경로에 존재하고 이동 여부가 명확하지 않으면 각각 별도의 `documents`로 유지한다.

```text
document A
revision hash = X

document B
revision hash = X
```

는 정상 데이터다.

---

## 24.5 삭제

첫 미발견:

```text
missing_since = now()
```

Grace Period 내 다시 발견:

```text
missing_since = NULL
```

Grace Period 후 계속 미발견:

```text
is_deleted = TRUE
deleted_at = now()
```

soft delete 이후:

* 일반 검색 제외
* RAG Retrieval 제외
* favorites/recent UI 제외
* historical revision과 chat source는 보존

실제 hard delete는 별도 retention 정책이 생기기 전까지 수행하지 않는다.

---

# 25. 검색 데이터 범위

일반 검색 및 신규 RAG의 검색 대상은 다음 조건을 모두 만족해야 한다.

```text
1. documents.is_deleted = FALSE

2. document_revisions.id
   = documents.current_revision_id

3. document_revisions.is_ready = TRUE

4. 현재 사용자에게 document READ 이상 권한 존재
```

즉:

```text
Current READY Revision
+
ACL
```

이 검색 후보의 절대 조건이다.

---

# 26. Lexical Search 정책

`chunks.search_vector`는 PostgreSQL FTS 계열 방식을 선택할 경우 사용한다.

한국어 검색 PoC에서 다음을 비교한다.

```text
A. PostgreSQL simple FTS

B. simple FTS
   + pg_trgm

C. PGroonga
   + pgvector
```

PoC 결과에 따라:

* `search_vector` 생성 방식
* GIN index
* pg_trgm index
* PGroonga index

중 최종 구성을 결정한다.

### 주의

Text Search configuration을 변경한다고 기존 `search_vector` 값이 자동 변경되는 것은 아니다.

설정 변경 시 해당 revision/chunk의 lexical index를 재생성해야 한다.

PGroonga 채택 시 `search_vector` 컬럼 자체를 실제 검색에 사용하지 않을 수도 있다.

---

# 27. Embedding 정책

Embedding Provider와 Model은 별도 관리한다.

Revision에 다음을 기록한다.

```text
embedding_provider
embedding_model
embedding_dimension
embedding_version
embedded_at
```

초기에는 exact vector search를 baseline으로 한다.

Corpus 규모 및 latency 문제가 커지면 HNSW 적용을 검토한다.

Embedding 모델 변경 시:

```text
REINDEX job
↓
해당 revision의 chunk embedding 재생성
```

을 수행한다.

### Embedding Dimension

현재 DDL:

```sql
VECTOR(1536)
```

은 예시다.

Embedding 모델 선정 후 실제 차원으로 migration한다.

차원이 변경되는 모델로 교체하는 경우 DB column migration도 필요하다.

---

# 28. Parser / Chunking 정책

Revision 생성 이후 다음 provenance를 기록한다.

```text
parser_name
parser_version
chunking_version
```

MVP 단계에서는 Parser 및 Chunking 방식을 PoC 후 고정한다.

단순 재임베딩은 기존 chunk를 유지한다.

Chunking 전략을 변경해야 하는 경우 기존 chunk를 덮어쓰지 않는다.

이는 다음 이유 때문이다.

```text
과거 chat_message_sources
→ 기존 chunk_id
```

가 이미 존재할 수 있기 때문이다.

Chunking generation 자체를 버전별로 병행하는 기능은 MVP 이후 별도 설계한다.

---

# 29. 데이터 삭제 정책

## Documents

기본은 soft delete.

## Revisions

과거 provenance 보존을 위해 기본적으로 삭제하지 않는다.

## Chunks

과거 chat source가 참조한 chunk는 삭제할 수 없다.

## Chat

사용자/회사 retention 정책에 따라 별도 결정한다.

## Audit Log

보안정책에서 정의한 retention 기간을 적용한다.

---

# 30. 보고서 생성 관련 확장

템플릿 기반 보고서 자동 생성은 기능 명세상 P3(MVP+2)이다.

따라서 현재 MVP DB에는 다음 테이블을 생성하지 않는다.

```text
report_templates
generated_reports
generated_report_sources
```

후속 단계에서 추가한다.

기존 구조를 그대로 재사용할 수 있다.

```text
generated_report_sources
      │
      └── chunks
              │
              └── document_revisions
                      │
                      └── documents
```

따라서 보고서 생성 기능 추가를 위해 현재 문서/Revision/Chunk 스키마를 변경할 필요는 없도록 한다.

---

# 31. 현재 MVP 테이블 목록

```text
1. departments

2. users

3. documents

4. document_revisions

5. chunks

6. document_permissions

7. processing_jobs

8. tags

9. document_tags

10. revision_tags

11. favorites

12. recent_views

13. chat_sessions

14. chat_messages

15. chat_message_sources

16. audit_logs
```

총 **16개 테이블**이다.

---

# 32. 구현 전 남은 결정 사항

## P0

### 1. 외부 AI 사용 정책

확인 대상:

```text
LLM API
Embedding API
Claude CLI / Code
```

---

## P1 기술 PoC

### 2. HWP/HWPX Parser

확정 항목:

```text
parser_name
parser_version
라이선스
표 구조 추출
페이지/문단 anchor
```

### 3. 한국어 Lexical Search

비교:

```text
PostgreSQL simple
simple + pg_trgm
PGroonga
```

---

## P1 구현 전 결정

### 4. Embedding Model

결정 후:

```text
VECTOR(N)
```

dimension 확정.

### 5. Processing Retry Backoff

예:

```text
1차 실패 → 즉시 또는 짧은 지연
2차 실패 → 수십 초
3차 실패 → 수분

최종 실패 → FAILED
```

구체적인 값은 Worker 구현 단계에서 결정한다.

### 6. File Missing Grace Period

예:

```text
수 시간
1일
수일
```

공유폴더 운영 방식 확인 후 결정한다.

### 7. HNSW 적용 기준

문서 수 자체가 아니라 실제 측정한:

```text
chunk 수
검색 latency
RAM 사용량
Recall
```

을 기준으로 결정한다.

---

# 33. MVP DB 완료 기준

다음 조건을 만족하면 DB 설계 완료로 본다.

1. 문서와 콘텐츠 revision이 분리된다.
2. 동일 hash 파일을 여러 문서에 저장할 수 있다.
3. 과거 revision은 덮어쓰지 않는다.
4. 최신 발견 revision과 검색 가능한 revision을 구분한다.
5. 다른 document의 revision을 current revision으로 지정할 수 없다.
6. 일반 검색은 current READY revision만 사용한다.
7. 동일 revision에 동일 chunk index가 중복 생성되지 않는다.
8. 사용자/부서 ACL이 실제 FK로 보장된다.
9. 권한 없는 document의 chunk가 검색 후보에 포함되지 않는다.
10. AUTO 태그가 생성 당시 revision과 연결된다.
11. MANUAL 태그가 revision 변경 후에도 document에 유지된다.
12. 과거 RAG 답변이 사용한 chunk를 추적할 수 있다.
13. 과거 답변 source의 revision을 재구성할 수 있다.
14. Parser/Embedding/LLM 처리 버전을 추적할 수 있다.
15. File Sync 재처리 시 중복 데이터 생성이 방지된다.
16. Soft delete된 문서는 검색되지 않지만 과거 provenance는 보존된다.
17. `recent_views`와 실제 감사 로그의 역할이 분리된다.
18. 향후 보고서 생성 기능에서 현재 chunk/revision provenance 구조를 그대로 사용할 수 있다.

---

# 34. 최종 핵심 구조

```text
                         ┌──────────────────┐
                         │   departments    │
                         └────────┬─────────┘
                                  │
                         ┌────────▼─────────┐
                         │      users       │
                         └────────┬─────────┘
                                  │
                                  │
┌─────────────────────────────────▼────────────────────────┐
│                       documents                         │
│                                                       │
│ latest_revision_id  → 가장 최근 발견본                  │
│ current_revision_id → 가장 최근 검색 가능 READY 본      │
└───────────────────────────┬────────────────────────────┘
                            │
                            ▼
                 ┌──────────────────────┐
                 │ document_revisions   │
                 │                      │
                 │ immutable content    │
                 │ processing status    │
                 │ parser provenance    │
                 │ embedding provenance │
                 └──────────┬───────────┘
                            │
                            ▼
                     ┌────────────┐
                     │   chunks   │
                     └─────┬──────┘
                           │
          ┌────────────────┴────────────────┐
          │                                 │
          ▼                                 ▼
   Hybrid Search                      RAG Retrieval
          │                                 │
          │                                 ▼
          │                        chat_message_sources
          │                                 │
          └─────────────────────────────────┘

모든 검색 경로:

Identity
→ ACL
→ Current READY Revision
→ Chunk Retrieval
```

---

# 35. 최종 설계 원칙

> **공유폴더는 현재 원본의 Source of Truth이고, DB의 revision/chunk는 검색과 RAG 결과를 재현하기 위한 불변 provenance 계층이다.**

> **최신 파일이 처리 중이어도 기존 READY revision은 계속 검색 가능해야 한다.**

> **ACL은 검색 결과를 가리는 후처리가 아니라 검색 후보를 만들기 전 적용되는 보안 조건이다.**

> **과거 답변의 근거로 사용된 chunk는 문서가 수정된 이후에도 식별 가능해야 한다.**

> **AI가 생성한 요약·태그·답변은 원본 정보와 구분하며, 언제 어떤 parser/model/prompt로 만들어졌는지 추적할 수 있어야 한다.**

> **보고서 자동 생성은 이 provenance·ACL·retrieval 기반이 안정화된 후 동일한 문서 인프라 위에 추가한다.**
