# 사내 문서 관리 시스템 — DB 스키마 (v2.3)

> **v2.2 대비 주요 변경**
>
> 1. HWP/HWPX Parser PoC 결과 반영 (`docs/poc/hwp-poc-report.md`)
> 2. `document_revisions.parse_result_code` 추가 — 실행 상태와 파싱 결과 의미 분리
> 3. `embedding_status` / `summary_status` / `tagging_status`에 `SKIPPED` 추가
> 4. `is_ready` 조건에 `parse_result_code = 'TEXT_EXTRACTED'` 추가
> 5. `processing_jobs.result_code` 추가 — 운영 통계·재시도 판단용 구조화 코드
> 6. `chunks.page_number` / `chunks.section_title`에 HWP/HWPX 확보 불가 주석 명시
> 7. `current_revision_id` 승격 조건에 `parse_result_code` 반영
> 8. Parser provenance 기록 의무 명시 (§28)
> 9. 향후 ACL DENY/override 확장 가능성 기록 (§11)
> 10. extracted content의 Object Storage 이전 계획 기록 (§7)
>
> **DDL 변경은 위 2·3·4·5뿐이며 테이블 개수는 16개로 동일하다.**
> Migration SQL은 본 문서에서 작성하지 않는다.
>
> v2.2 문서(`docs/database_schema.MD`)는 설계 이력으로 그대로 보존한다.

---

> **v2.1 대비 주요 변경 (v2.2에서 도입, 이력 보존)**
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

    /*
     * parsed_structure
     * 표/문단 등의 구조화 결과.
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

운영 대시보드에서 파싱 결과 분포(OCR_REQUIRED / ENCRYPTED / CORRUPT 등)를
집계하기 위한 인덱스:

```sql
CREATE INDEX idx_revisions_parse_result
    ON document_revisions(parse_result_code)
    WHERE parse_result_code IS NOT NULL;
```

### Parsed Content 저장 정책

MVP에서는 `extracted_text`와 `parsed_structure`를 PostgreSQL에 직접 저장한다.

```text
MVP:
PostgreSQL document_revisions에 직접 저장

문서량/용량 증가 시:
Object Storage / NFS 등으로 extracted content를 이동하고
revision에는 URI + hash + provenance만 유지하는 방식 검토
```

**이번 단계에서 Object Storage를 도입하지 않으며 URI 컬럼도 미리 추가하지 않는다.**

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
revision.parse_status      = SUCCESS
AND
revision.parse_result_code = TEXT_EXTRACTED
AND
revision.embedding_status  = SUCCESS
AND
document.is_deleted        = FALSE
```

이는 `revision.is_ready = TRUE`와 동일한 조건이다.

본문을 확보하지 못한 revision(`OCR_REQUIRED`, `ENCRYPTED`, `EMPTY_DOCUMENT` 등)은
`current_revision_id`로 승격하지 않는다.
이 경우 기존 READY revision이 계속 검색 대상으로 유지된다.

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
```

```sql
CREATE INDEX idx_chunks_revision
    ON chunks(document_revision_id);
```

### Citation Anchor

포맷별 primary anchor는 기능 명세 §14.1에서 정의한다.

```text
HWP 5.x / HWPX
→ paragraph_start / paragraph_end 가 primary anchor
→ section_title은 있을 때만 부가 정보
→ page_number는 일반적으로 NULL

PDF / DOCX
→ 각 parser PoC 이후 확정
```

`page_number`가 NULL인 것은 오류가 아니라 HWP/HWPX의 정상 상태다.

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

### 향후 확장 (MVP 범위 아님)

향후 다음과 같은 요구가 생길 수 있다.

```text
부서 전체 READ
+
특정 사용자만 접근 금지
```

이는 현재의 allow-only 모델로 표현할 수 없으므로 명시적 DENY / override 정책이 필요하다.

**이번 스키마에서는 DENY를 구현하지 않는다.**

향후 추가할 경우 반드시 다음을 먼저 정의해야 한다.

```text
- DENY와 ALLOW의 precedence
- 사용자 DENY와 부서 ALLOW가 충돌할 때의 우선순위
- ADMIN 권한이 DENY를 무시하는지 여부
```

precedence를 정의하지 않은 채 DENY를 추가하면 검색 선필터링 결과가
예측 불가능해진다.

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

실패 유형 집계용:

```sql
CREATE INDEX idx_jobs_result_code
    ON processing_jobs(
        job_type,
        result_code
    )
    WHERE result_code IS NOT NULL;
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

`parser_name`과 `parser_version`은 revision 생성 결과와 함께 **반드시 기록한다.**

Parser를 교체하거나 개선한 이후에도 과거 revision의 파싱 결과를 재현할 수 있어야 하며,
어떤 parser로 만들어진 결과인지 알 수 없으면 재현이 불가능하다.

HWP/HWPX PoC에서 동일 입력에 대한 파싱 결과가 결정적임을 확인했다.
프로세스를 달리한 5회 반복에서도 canonical parsing 결과가 동일했으므로,
parser 버전이 고정되어 있으면 과거 결과를 재현할 수 있다.

## Chunking 방향 (미확정)

HWP/HWPX Parser PoC에서 표의 row/column/cell 구조 보존 가능성을 확인했다.

따라서 chunking 단계에서는 paragraph-aware chunking과 함께
table-aware chunking을 우선 검토한다.

구체적인 chunking policy는 Search PoC 및 Embedding 모델 선정 전에 별도 결정한다.

**이번 버전에서는 chunk schema를 변경하지 않는다.**
`content_type` 등 table chunk용 컬럼은 chunking policy 확정 후 추가한다.

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

### 2. HWP/HWPX Parser — **완료**

`docs/poc/hwp-poc-report.md` 참조. 결론: PASS WITH LIMITATIONS.

확정된 항목:

```text
parser_name        inhouse-hwp5 / inhouse-hwpx
parser_version     0.1.0
라이선스            production 의존성은 olefile(BSD-2-Clause) 1개. 라이선스 리스크 없음
표 구조 추출        row/column/cell 및 병합 span 보존 가능
문단 anchor        확보 가능 — HWP/HWPX primary anchor로 확정
페이지 anchor       확보 불가 — 포맷이 페이지 번호를 저장하지 않음
```

### 2-1. 사내 corpus 재측정 — **미완료 (선행 필수)**

PoC는 공개 third-party corpus 98건으로 수행했다.

사내 문서로 재측정하여 다음을 확인하기 전까지
HWP 5.x를 production ingestion 대상으로 확정하지 않는다.

```text
사내 문서 파싱 성공률
배포용/암호화 HWP 비율
표를 포함한 HWP 문서의 처리 품질
worker memory limit / 최대 허용 파일 크기 / 동시 처리 개수
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
    READY는 `parse_status = SUCCESS` + `parse_result_code = TEXT_EXTRACTED`
    + `embedding_status = SUCCESS`를 모두 만족하는 상태다.
7. 동일 revision에 동일 chunk index가 중복 생성되지 않는다.
8. 사용자/부서 ACL이 실제 FK로 보장된다.
9. 권한 없는 document의 chunk가 검색 후보에 포함되지 않는다.
10. AUTO 태그가 생성 당시 revision과 연결된다.
11. MANUAL 태그가 revision 변경 후에도 document에 유지된다.
12. 과거 RAG 답변이 사용한 chunk를 추적할 수 있다.
13. 과거 답변 source의 revision을 재구성할 수 있다.
14. Parser/Embedding/LLM 처리 버전을 추적할 수 있다.
15. worker 실행 상태(`parse_status`)와 파싱 결과 의미(`parse_result_code`)를 구분할 수 있다.
16. 본문을 확보하지 못한 revision의 downstream 작업이 `SKIPPED`로 구분되어
    실패(`FAILED`)와 혼동되지 않는다.
17. 파싱/처리 실패 유형을 구조화된 코드로 집계할 수 있다.
18. File Sync 재처리 시 중복 데이터 생성이 방지된다.
19. Soft delete된 문서는 검색되지 않지만 과거 provenance는 보존된다.
20. `recent_views`와 실제 감사 로그의 역할이 분리된다.
21. 향후 보고서 생성 기능에서 현재 chunk/revision provenance 구조를 그대로 사용할 수 있다.

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

---

# 36. v2.3 DDL 변경 요약

v2.2 대비 실제 DDL 변경은 다음 4가지뿐이다.
테이블은 추가·삭제되지 않았고 총 16개로 동일하다.

## 36.1 document_revisions

```sql
-- 신규 컬럼
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
    )
```

```sql
-- CHECK 확장: 'SKIPPED' 추가
embedding_status IN ('PENDING','RUNNING','SUCCESS','FAILED','SKIPPED')
summary_status   IN ('PENDING','RUNNING','SUCCESS','FAILED','SKIPPED')
tagging_status   IN ('PENDING','RUNNING','SUCCESS','FAILED','SKIPPED')
```

`parse_status`는 변경하지 않는다(4값 유지).

```sql
-- 생성 컬럼 정의 변경
is_ready BOOLEAN GENERATED ALWAYS AS (
    parse_status = 'SUCCESS'
    AND COALESCE(parse_result_code, '') = 'TEXT_EXTRACTED'
    AND embedding_status = 'SUCCESS'
) STORED
```

```sql
-- 신규 인덱스
CREATE INDEX idx_revisions_parse_result
    ON document_revisions(parse_result_code)
    WHERE parse_result_code IS NOT NULL;
```

## 36.2 processing_jobs

```sql
-- 신규 컬럼
result_code TEXT
```

`error_message`는 그대로 유지한다.

```sql
-- 신규 인덱스
CREATE INDEX idx_jobs_result_code
    ON processing_jobs(job_type, result_code)
    WHERE result_code IS NOT NULL;
```

## 36.3 주석만 변경 (DDL 영향 없음)

```text
chunks.page_number     HWP/HWPX에서 확보 불가 — 일반적으로 NULL
chunks.section_title   optional enhancement — 추정 금지
```

컬럼은 **제거하지 않는다.**

## 36.4 이번 버전에서 변경하지 않은 것

다음은 후속 PoC/설계 단계의 결정 사항이므로 v2.3에서 확정하지 않았다.

```text
VECTOR(1536) 최종 확정
한국어 FTS 방식 확정
PGroonga 적용
HNSW 인덱스 생성
chunks.content_type 컬럼
table chunk schema
extracted content의 Object Storage URI 컬럼
ACL DENY 규칙
report_templates / generated_reports 등 보고서 테이블
document_revisions.normalizer_version
```

Migration SQL은 본 문서에서 작성하지 않는다.

---

# 37. Open Issues Disposition (DB 영향분)

전체 disposition 표는 기능 명세 v2.3 §28.1에 있다.
아래는 DB 스키마에 영향을 준 항목만 옮긴 것이다.

| ID | Issue | Decision | DB 반영 |
| --- | --- | --- | --- |
| OI-1 | `OCR_REQUIRED`를 저장할 상태값 없음 | **FIX NOW** | `document_revisions.parse_result_code` 추가 (§36.1) |
| OI-2 | 구조화된 실패·결과 코드 없음 | **FIX NOW** | `processing_jobs.result_code` 추가 (§36.2) |
| OI-3 | `page_number` 기반 citation 실현 불가 | **FIX NOW** | `chunks.page_number` 주석 명시. 컬럼 유지 (§36.3) |
| OI-4 | normalizer 버전 기록 컬럼 없음 | **DEFER** | 변경 없음. chunking policy 확정 시 재검토 (§28) |
| OI-5 | 배포용 HWP 처리 정책 부재 | **FIX NOW** (일부 DEFER) | `parse_result_code = 'ENCRYPTED'`로 표현 가능. 추가 DDL 없음 |
| OI-6 | 확장자/실제 포맷 불일치 | **DEFER** | `parse_result_code = 'UNSUPPORTED_FORMAT'`으로 표현 가능. 추가 DDL 없음 |
