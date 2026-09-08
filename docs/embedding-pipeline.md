# Embedding Pipeline + Current Revision Promotion

구현: `src/ingestion/embedding.py`, `embedding_service.py`, `repository.py`
테스트: `tests/test_embedding.py`, `tests/test_embedding_pipeline.py`

기준 문서는 [기능명세 v2.4](functional-spec-v2.4.md), [DB 스키마 v2.5](database-schema-v2.5.md),
[Design Freeze v1](design-freeze-v1.md), [Ingestion Foundation](ingestion-foundation.md)다.
이번 구현으로 그 문서들과 migration을 수정하지 않았다.

```text
TEXT_EXTRACTED revision → EMBED job → chunk 벡터 → VECTOR(384)
    → embedding_status=SUCCESS → is_ready=TRUE → current_revision_id 승격
```

Search / API / RAG는 이 단계에 없다.

---

## 1. EMBED job lifecycle

```text
enqueue   PARSE Tx B 안에서, result_code = TEXT_EXTRACTED 일 때만
claim     FOR UPDATE SKIP LOCKED 로 PENDING → RUNNING
running   모델 추론 (트랜잭션 밖)
success   벡터 + provenance + status + job + promotion  (단일 트랜잭션)
failure   embedding_status=FAILED, job FAILED, current 불변
retry     reset_job_for_retry() → PENDING 재큐. 재실행 시 벡터 전체 교체
```

enqueue를 PARSE와 같은 트랜잭션에 넣은 이유는, "chunk는 쓰였는데 임베딩 job은 없는"
상태로 revision이 고립되지 않게 하기 위함이다. 이 시점에 모델은 호출하지 않는다.

다음 result code에는 EMBED job이 생기지 않고 `embedding_status = SKIPPED`가 유지된다.

```text
EMPTY_DOCUMENT  OCR_REQUIRED  ENCRYPTED  CORRUPT  UNSUPPORTED_FORMAT  PARSE_FAILED
```

중복 방지는 두 겹이다. `enqueue_embed_job`의 WHERE 절이 이미 임베딩됐거나 본문이 없는
revision을 거르고, 스키마의 `uq_jobs_active` 부분 UNIQUE 인덱스가 같은 revision에 대한
두 번째 활성 job을 거부한다. 반복 스캔·parse 재시도·동시 worker가 모두 하나로 수렴한다.

`processing_jobs.result_code`(EMBED 단계):

```text
EMBEDDED  NO_CHUNKS  MODEL_UNAVAILABLE  INVALID_VECTOR  EMBED_FAILED
```

---

## 2. Embedding

| 항목 | 값 |
| --- | --- |
| model | `intfloat/multilingual-e5-small` |
| revision | `614241f622f53c4eeff9890bdc4f31cfecc418b3` (고정) |
| dimension | 384 |
| provider | `local` |
| prefix | `passage: ` (문서). `query: `는 검색 단계용으로 정의만 |
| normalization | L2 (모델에 `normalize_embeddings=True`) |
| device | CPU |
| batch | 기본 8 (`EMBEDDING_BATCH_SIZE`) |

모델 revision을 고정한 이유는, 업스트림이 조용히 갱신되면 이미 임베딩된 코퍼스와
새 벡터가 다른 공간에 놓이기 때문이다. 변경은 의도적 reindex여야 한다.

`passage: ` 접두사는 **모델 래퍼(`LocalE5Model`)가 붙인다.** service는 chunk 원문을
그대로 넘긴다. 접두사는 e5 고유 규약이라, 다른 모델로 바꿀 때 orchestration 코드가
e5 의미를 품고 있으면 안 된다.

### 로컬 실행 강제

외부 embedding API를 쓰지 않는다. 기본값으로 **런타임 모델 다운로드도 하지 않는다**
(`EMBEDDING_ALLOW_DOWNLOAD=0`). 로더에 `local_files_only=True`를 주고 동시에
`HF_HUB_OFFLINE=1`을 설정해 전송 자체가 열리지 않게 한다.

모델이 로컬에 없으면 조용히 대체하지 않고 명확히 실패한다.

```text
embedding model 'intfloat/multilingual-e5-small' could not be loaded locally
(revision=..., download allowed=False). Pre-populate the model cache or set
EMBEDDING_ALLOW_DOWNLOAD=1 deliberately.
```

모델은 worker당 1회 지연 로딩된다. chunk마다 로드하면 배치 시간을 지배한다.

### 설정

```text
EMBEDDING_MODEL            EMBEDDING_MODEL_REVISION
EMBEDDING_DIMENSION        EMBEDDING_DEVICE
EMBEDDING_BATCH_SIZE       EMBEDDING_CACHE_DIR
EMBEDDING_ALLOW_DOWNLOAD
```

`EMBEDDING_DIMENSION`이 384가 아니면 **startup에서 거부한다.** 컬럼이 `VECTOR(384)`인데
설정이 어긋나면 배치 중간에 행 단위로 실패하고, 그때는 어디까지 썼는지 파악하기 어렵다.

---

## 3. Transaction boundary

```text
[Tx] claim: PENDING → RUNNING                         COMMIT
     ↓
     chunk 로드 → 모델 추론 → 전량 검증               (트랜잭션 없음)
     ↓
[Tx] clear → 벡터 write → provenance → status
     → job 종료 → promotion                           COMMIT
```

추론은 느리므로 그동안 DB 락을 잡지 않는다.

### partial embedding 방지

chunk 10개 중 7개만 성공했는데 `embedding_status = SUCCESS`가 되면, `is_ready`가 참이
되어 **벡터가 없는 chunk를 가진 revision이 검색에 노출된다.**

구조적으로 막았다. 모든 chunk를 임베딩하고 **전부 검증한 뒤에야** 쓰기 트랜잭션을
연다. 어느 하나라도 실패하면 그 전에 예외가 나므로 부분 상태가 만들어질 수 없다.
쓰기는 단일 트랜잭션이라 중간 실패는 통째로 롤백된다.

---

## 4. Vector validation

DB에 쓰기 전에 순서대로 검사한다.

| 검사 | 거부 사유 |
| --- | --- |
| dimension | 384가 아니면 거부. pgvector도 거부하겠지만 여기서 잡아야 배치 중간이 아니라 명확한 메시지가 남는다 |
| finite | NaN/Inf는 그 벡터와의 모든 cosine 거리를 조용히 오염시킨다 |
| L2 norm | `abs(norm - 1) > 1e-3`이면 거부. 정규화가 아예 안 된 모델을 잡는다 |

검증 실패는 `INVALID_VECTOR`로 job을 FAILED 처리하고 아무것도 쓰지 않는다.

---

## 5. READY

`is_ready`는 **DB의 생성 컬럼**이며 애플리케이션이 쓰지 않는다.

```sql
parse_status = 'SUCCESS'
AND COALESCE(parse_result_code,'') = 'TEXT_EXTRACTED'
AND embedding_status = 'SUCCESS'
```

코드에 별도의 READY boolean을 두지 않는다. 두 개를 두면 언젠가 어긋난다.
`embedding_status`를 SUCCESS로 바꾸는 것만으로 DB가 재계산한다.

---

## 6. Current revision promotion

**"방금 끝난 revision"을 current로 두지 않는다.**

```sql
SELECT id FROM document_revisions
WHERE document_id = ? AND is_ready = TRUE
ORDER BY revision_no DESC
LIMIT 1
```

current는 "마지막으로 끝난 job"이 아니라 **문서의 속성**이다. 임베딩은 순서를 지키지
않고 끝나므로, 느린 revision 1이 revision 2보다 늦게 끝났다고 current를 1로 되돌리면
안 된다. 위 질의는 그 상황에서 자연히 2를 유지한다.

승격 직전 `SELECT ... FROM documents WHERE id = ? FOR UPDATE`로 문서 행을 잠근다.
같은 문서에 대한 두 승격이 경쟁해 오래된 값으로 덮어쓰는 것을 막는다.

승격은 임베딩 성공과 **같은 트랜잭션**에서 실행되므로 `is_ready`를 일관된 시점에서 본다.

### 시나리오

| 상황 | latest | current |
| --- | --- | --- |
| 최초 revision READY | rev1 | rev1 |
| rev2 파싱 완료, 임베딩 대기 | rev2 | **rev1** (검색 계속 가능) |
| rev2 임베딩 성공 | rev2 | rev2 |
| rev2 임베딩 실패 | rev2 | **rev1** (기존 문서 유지) |
| rev1이 rev2보다 늦게 완료 | rev2 | **rev2** (하향 없음) |
| rev3 대기 / rev2 READY | rev3 | **rev2** |

`current == latest`를 강제하지 않는다. 둘이 다른 것이 정상 상태다.

불변식:

```text
current_revision_id 는 같은 document 소속 (migration의 복합 FK가 강제)
current_revision_id 는 is_ready = TRUE
current_revision_id 는 READY revision 중 revision_no 최대
```

soft-deleted 문서도 provenance 유지를 위해 승격 자체는 허용한다. 검색 노출은 기존
`current_ready_chunks` + `is_deleted` 필터가 담당한다.

---

## 7. Failure / retry

한 revision의 실패가 worker를 죽이지 않는다. `psycopg.OperationalError`(DB 자체 장애)만
fail-fast하고, 나머지는 해당 revision만 FAILED로 기록하고 다음으로 넘어간다.

실패 시:

```text
embedding_status = FAILED     (is_ready 자동 FALSE)
processing_job   = FAILED + result_code + error_message
current_revision_id            변경 없음
chunk 벡터                      전부 NULL로 정리
```

`error_message`에 chunk 본문을 넣지 않는다. 예외 타입과 메시지만 남기고 2000자로 자른다.

재시도는 `reset_job_for_retry()`로 job을 PENDING으로 되돌린다. 재실행 시 쓰기
트랜잭션이 먼저 `clear_chunk_embeddings()`를 호출하므로 **옛 모델/부분 실행의 벡터가
새 벡터와 섞이지 않는다.** exponential backoff 스케줄러는 이번 범위가 아니다.

이미 `embedding_status = SUCCESS`인 revision에 job이 남아 있으면 재임베딩하지 않고
job만 정리한다.

---

## 8. CLI

```bash
export SHARED_ROOT=/mnt/shared
export DATABASE_URL=postgresql://user:pass@host:5432/dbname
export EMBEDDING_CACHE_DIR=/opt/models        # 사전 배치된 모델 캐시

python -m ingestion sync     # 발견
python -m ingestion parse    # PARSE job (EMBED job 큐잉 포함)
python -m ingestion embed    # EMBED job
python -m ingestion run      # 셋 다
```

출력은 건수뿐이다. 경로·본문을 찍지 않는다.

---

## 9. Known limitations

1. **retry backoff 스케줄러가 없다.** `next_attempt_at` 정책은 미구현이며 재큐만 가능하다.
2. **HNSW 인덱스가 없다.** 초기 검색은 exact cosine이다(Design Freeze).
3. **query 임베딩·검색이 없다.** `query: ` 접두사는 상수로만 정의돼 있다.
4. **batch size 8은 튜닝하지 않은 기본값이다.** 실제 코퍼스에서 재측정이 필요하다.
5. **모델을 사전 배치해야 한다.** 런타임 다운로드가 기본 비활성이라 배포 시 캐시가 필요하다.
6. **worker 동시성 운영 정책은 미정이다.** DB 제약으로 정합성은 지키지만 worker 수·주기는 OPEN이다.
7. **재임베딩 트리거가 없다.** 모델 교체 시 대량 재처리 경로는 별도 설계가 필요하다.

---

## 10. Architecture Notes

스키마를 변경하지 않고 기록만 한다.

### AN-1. EMBED 단계의 result_code 도메인이 스키마에 명시돼 있지 않다

DB v2.5는 `processing_jobs.result_code`에 대해 "PARSE job에서는
`document_revisions.parse_result_code`와 동일한 값 도메인을 사용하고,
EMBED/SUMMARIZE/TAG/REINDEX는 worker 구현 단계에서 확장한다"고 위임했다.

이번 구현이 사용하는 값은 다음 5종이다.

```text
EMBEDDED  NO_CHUNKS  MODEL_UNAVAILABLE  INVALID_VECTOR  EMBED_FAILED
```

컬럼에 CHECK 제약이 없으므로 DB 변경 없이 동작하지만, PARSE와 달리 **문서에 고정된
도메인이 없다.** 운영 대시보드가 이 값을 집계하려면 스키마 문서나 상수 정의 한쪽에
도메인을 명시하는 편이 안전하다. 이번에는 컬럼을 추가하거나 CHECK를 걸지 않았다.

### AN-2. 재임베딩(REINDEX) 경로가 없다

모델이나 dimension이 바뀌면 기존 revision 전체를 다시 임베딩해야 한다.
스키마에 `REINDEX` job type이 이미 있지만 이번 구현은 사용하지 않는다.
`embedding_version`으로 어떤 revision이 옛 모델로 임베딩됐는지 식별할 수는 있다.
대량 재처리 트리거·순서·부하 제어는 별도 설계가 필요하다.

### AN-3. `current_ready_chunks` VIEW는 ACL을 적용하지 않는다

설계대로다(DB v2.5 §10). 이 단계에서는 문제가 없지만, 검색 백엔드가 이 VIEW를 쓸 때
**반드시 ACL 선필터를 추가로 적용**해야 한다. VIEW 이름이 안전해 보인다는 이유로
그대로 노출하면 권한 없는 문서가 새어 나간다.
