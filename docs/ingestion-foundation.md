# File Sync + Ingestion Foundation

구현 위치: `src/ingestion/` · 테스트: `tests/test_file_scanner.py`, `test_file_sync.py`,
`test_ingestion.py`, `test_chunker.py`, `test_document_year.py`

기준 문서는 [기능명세 v2.4](functional-spec-v2.4.md), [DB 스키마 v2.5](database-schema-v2.5.md),
[Design Freeze v1](design-freeze-v1.md), [API Contract v1](api-contract-v1.md)다.
이번 구현으로 그 문서들을 수정하지 않았다.

```text
Shared Folder → Discovery → documents → document_revisions
              → PARSE job → Parser → chunks
```

Embedding, current revision 승격, Search, API는 **이 단계에 없다.**

---

## 1. Scan lifecycle

`SyncService.scan_once()` 한 번이 완전한 재조정이다. 데몬이나 watcher는 없다.

```text
1. scan_files()        shared root 아래 .hwp/.hwpx/.docx/.pdf 발견
2. fingerprint()       SHA-256 + 안정성 검사        (트랜잭션 밖)
3. Transaction A       document / revision / latest / PARSE job
4. _reconcile_missing  이번 스캔에서 안 보인 문서 표시 + grace 만료 처리
```

파싱은 별도 단계다.

```text
5. claim_parse_jobs()  PENDING → RUNNING (FOR UPDATE SKIP LOCKED)
6. parse_document()    기존 파서 호출                (트랜잭션 밖)
7. Transaction B       parse 결과 + chunks + job 종료
```

CLI:

```bash
export SHARED_ROOT=/mnt/shared
export DATABASE_URL=postgresql://user:pass@host:5432/dbname

python -m ingestion sync     # 발견만
python -m ingestion parse    # PARSE job만
python -m ingestion run      # 둘 다
```

출력은 **건수뿐**이다. 경로나 본문을 찍지 않는다.

---

## 2. File identity

| 항목 | 값 |
| --- | --- |
| 내용 식별 | SHA-256 (`document_revisions.content_hash`) |
| 문서 식별 | shared root 기준 **상대 POSIX 경로** (`documents.source_path`) |
| 절대 경로 | DB에 저장하지 않는다. 파일을 열 때만 메모리에서 사용 |

`content_hash`에 UNIQUE가 없는 것은 의도적이다. 같은 바이트가 서로 다른 경로에
독립 문서로 존재할 수 있다(A/file.hwp, B/copy.hwp).

### 반쯤 쓰인 파일 방지

공유폴더는 살아 있다. 누군가 저장하는 중에 스캔이 그 파일을 읽을 수 있다.
해시 전후로 `stat`(size/mtime/ctime)을 비교하고, 달라졌으면 이번 실행에서
처리하지 않는다. **부분 파일을 revision으로 저장하지 않는다.**

---

## 3. new / modified / unchanged / missing

| 상황 | 처리 |
| --- | --- |
| 새 경로 | document + revision(1) + latest + PARSE job |
| 같은 경로, 같은 hash | `last_seen_at`만 갱신. **revision 생성 안 함** |
| 같은 경로, 다른 hash | 같은 document에 revision +1, latest 이동, PARSE job |
| 안 보임 | `missing_since` 기록. grace 경과 후 soft delete |
| 다시 나타남 | `missing_since`/`is_deleted`/`deleted_at` 해제 |

mtime만 바뀌고 SHA-256이 같으면 revision을 만들지 않는다. 백업 복원처럼 메타데이터만
건드리는 작업 후 전체 코퍼스를 재파싱·재임베딩하는 사태를 막는다.

**hard delete는 없다.** revision, chunk, 과거 답변 provenance가 모두 남는다.

### move / rename 정책 — 보수적

같은 내용이 새 경로에 나타나고 기존 경로가 사라져도 **자동 병합하지 않는다.**

```text
기존 경로 → missing
새  경로 → 새 logical document
```

해시만으로 병합하면 서로 무관한 복사본 두 개의 이력이 합쳐지고, 이는 되돌릴 수 없다.
새 문서를 만드는 쪽은 나중에 합칠 수 있는 실수다. **알 수 없으면 안전한 쪽을 택한다.**

한계: 대규모 디렉터리 재편 시 문서가 중복 생성된다. 이동 추론은 경로 유사도·inode 등
추가 신호가 필요하며 이번 범위 밖이다.

---

## 4. Transaction boundary

```text
Transaction A   document lookup/create → revision → latest → PARSE job   COMMIT
                (해시 계산은 이 밖에서 끝난 상태)

parser 실행     (트랜잭션 없음. 느린 작업으로 락을 잡지 않는다)

Transaction B   parse 결과 + chunks 재작성 + job 종료                     COMMIT
```

A가 한 트랜잭션인 이유: 중간에 죽어도 "revision은 있는데 처리할 job이 없는" 상태가
생기지 않는다. B가 한 트랜잭션인 이유: chunk만 쓰이고 상태가 안 바뀌거나 그 반대가
되지 않는다.

파일 하나당 짧은 트랜잭션을 쓴다. 스캔 전체를 한 트랜잭션으로 묶으면 900번째 파일의
실패가 앞의 899건을 롤백시킨다.

---

## 5. Parse lifecycle

파서는 `src/document_processing`의 기존 구현을 **그대로 호출**한다. 재작성·복사 없음.

```text
parse_status        PENDING → RUNNING → SUCCESS | FAILED     (worker 실행 상태)
parse_result_code   파싱 결과의 의미                          (7종 closed set)
```

`parse_status = SUCCESS` + `parse_result_code = OCR_REQUIRED`는 정상이다.
파서가 스캔 문서임을 **정확히 판정**했기 때문이다.

`PARSE_FAILED`만 worker 실패다. 예상치 못한 파서 예외는 프로세스를 죽이지 않고
`parse_status=FAILED` / `parse_result_code=PARSE_FAILED`로 기록된다.

### 저장 필드

| 컬럼 | 값 |
| --- | --- |
| `extracted_text` | `build_normalized_text()` 결과. lexical 검색 대상 |
| `parsed_structure` | `canonicalize()` 결과. **표 row/column/cell/span 보존** |
| `parser_name` / `parser_version` | 파서가 보고한 값 |
| `chunking_version` | TEXT_EXTRACTED일 때만 기록 |

### downstream 상태

```text
TEXT_EXTRACTED   → embedding/summary/tagging = PENDING   (임베딩은 다음 단계)
그 외 6종        → embedding/summary/tagging = SKIPPED
```

임베딩을 하지 않았으므로 `embedding_status`를 SUCCESS로 만들지 않는다.
그렇게 하면 `is_ready`가 참이 되어 **벡터 없는 revision이 검색에 노출된다.**

---

## 6. Chunk lifecycle

`parse_result_code = TEXT_EXTRACTED`일 때만 청킹한다.

| 항목 | 값 |
| --- | --- |
| strategy | paragraph-aware |
| target / hard max | **64 tokens (provisional)** |
| overlap | 0 |
| independent table-aware | OFF |
| anchor | `paragraph_start` / `paragraph_end` (parser의 실제 문단 index) |

64는 짧은 synthetic 코퍼스에서 나온 **구현 기본값**이며 사내 장문 문서의 최적값이
아니다. 하드코딩이 아니라 `CHUNK_MAX_TOKENS` 등 설정이다.

문단 경계를 최대한 유지한다. 다음 문단을 넣으면 예산을 넘을 때만 자른다.
**단일 문단이 예산을 넘을 때만** 내부 분할하며, 그 조각들은 모두 원래 문단 index를
`paragraph_start = paragraph_end`로 유지한다 — 분할해도 출처가 남는다.

문단 경계는 파서가 준 `paragraph_index`다. Search PoC의 `"다. "` 기준 가상 문단 분할은
production에 쓰지 않는다.

### 표

독립 TABLE chunk를 만들지 않는다(Design Freeze). 다만 표를 **버리지도 않는다** —
읽기 순서상 위치에 텍스트 블록으로 넣어 셀 내용이 검색 가능하게 하고,
구조 자체는 `parsed_structure`에 그대로 보존한다.

### 재처리 정책 — delete-then-insert

같은 revision을 다시 파싱하면 기존 chunk를 **모두 지우고 새로 쓴다.** upsert가 아니다.
재파싱 결과가 더 적은 chunk를 만들면 upsert는 이전 실행의 꼬리 chunk를 남기고,
낡은 chunk와 새 chunk가 섞인다. 두 문장 모두 Transaction B 안에서 실행된다.

### Tokenizer

청크 크기는 임베딩 모델(`intfloat/multilingual-e5-small`)의 tokenizer로 센다.
**모델 가중치는 로드하지 않는다** — 토큰 개수만 필요하다. tokenizer는 지연 로딩이며
`Tokenizer` 프로토콜로 주입 가능하다(테스트는 오프라인 `SimpleTokenizer` 사용).

---

## 7. document_year

`document_revisions.document_year` = **그 revision의 내용이 가리키는 연도**.

`source_mtime`이나 `created_at`을 쓰지 않는다. 2026년 계획서가 2027년에 수정될 수 있고,
2019년 문서가 오늘 처음 발견될 수 있다.

규칙 기반이며 NLP/LLM을 쓰지 않는다.

```text
1. 제목에서 앞뒤에 숫자가 붙지 않은 4자리 수를 모두 수집
2. 1900~2100 범위만 후보
3. 서로 다른 후보가 정확히 1개  → 그 연도
4. 2개 이상                    → NULL (모호)
5. 없음                        → NULL
```

| 제목 | 결과 |
| --- | --- |
| `2026년 사업계획서` | 2026 |
| `2025_보안운영지침` | 2025 |
| `회의록_최종` | NULL |
| `2025-2026 사업계획` | NULL (모호) |
| `1800년 자료` | NULL (범위 밖) |
| `20260908_백업` | NULL (4자리 경계 아님) |

**애매하면 NULL이다.** 틀린 연도는 없는 연도보다 나쁘다 — `year=` 필터에서 문서가
조용히 사라지고 사용자는 누락을 알아챌 방법이 없다.

---

## 8. Idempotency

같은 스캔을 반복해도 documents / revisions / jobs / chunks가 늘지 않는다.

| 중복 | 방지 수단 |
| --- | --- |
| 문서 | `documents.source_path` (활성 문서 UNIQUE) |
| revision | 같은 `content_hash`면 생성 안 함 |
| PARSE job | 스키마의 `uq_jobs_active` 부분 UNIQUE 인덱스 + `ON CONFLICT DO NOTHING` |
| revision_no 경쟁 | `UNIQUE (document_id, revision_no)`, 번호는 INSERT 안에서 계산 |
| chunk | `(document_revision_id, chunk_index)` UNIQUE + delete-then-insert |

동시성은 DB 제약 + 트랜잭션으로만 막는다. 분산 락은 도입하지 않았다.
job 획득은 `FOR UPDATE SKIP LOCKED`라 여러 worker가 같은 job을 집지 않는다.

---

## 9. Security

| 항목 | 처리 |
| --- | --- |
| 공유폴더 쓰기 | 없음. 읽기와 `stat`만 한다 |
| symlink | 기본 **미추적**. 심볼릭 디렉터리는 walk에서 제외 |
| `..` traversal | resolve 후 root 내부인지 검사, 아니면 `PathOutsideRootError` |
| 저장된 경로로 파일 열기 | **읽는 시점에 root 경계를 다시 검사** (`source_path`는 데이터다) |
| 절대 경로 | DB/CLI 출력에 넣지 않는다 |
| 로그 | 건수·result code·file_type만. 본문과 경로를 남기지 않는다 |
| 오류 메시지 | root 밖 경로를 담지 않는다 (그 경로 자체가 민감할 수 있다) |
| 외부 호출 | 없음. LLM/임베딩 API를 호출하지 않는다 (테스트로 강제) |

---

## 10. Known limitations

1. **move/rename을 문서 이동으로 추론하지 않는다.** 디렉터리 재편 시 문서가 중복 생성된다(§3).
2. **실시간 감지가 없다.** `scan_once()`뿐이며 watcher/스케줄러는 다음 단계다.
3. **retry backoff 스케줄러가 없다.** `reset_job_for_retry()`로 재큐만 가능하고
   `next_attempt_at` 정책은 미구현이다.
4. **64 tokens는 잠정값이다.** 사내 장문 문서에서 재측정이 필요하다.
5. **`document_year`는 제목만 본다.** 문서 내부 메타데이터나 본문은 보지 않는다.
6. **DOCX/PDF는 발견되지만 파싱되지 않는다.** `UNSUPPORTED_FORMAT`으로 분류된다.
7. **동시 worker 운영 정책은 미정이다.** DB 제약으로 정합성은 지키지만 worker 수·주기는 OPEN이다.

---

## 11. Architecture Notes

설계 문서를 수정하지 않고 기록만 한다.

### AN-1. ingestion 이벤트를 위한 audit_logs taxonomy가 없다

기능명세 §21.2가 정의한 action은 `DOCUMENT_VIEW`, `DOCUMENT_DOWNLOAD`, `SEARCH`,
`TAG_UPDATE`, `PERMISSION_UPDATE`, `RAG_QUERY`, `REPORT_GENERATE`, `REPROCESS`로
**전부 사용자 행위**다. File Sync/Ingestion 이벤트에 해당하는 값이 없고
`audit_logs.actor_user_id`도 시스템 작업에는 맞지 않는다.

따라서 이번 구현은 **audit_logs에 아무것도 쓰지 않는다.** 새 taxonomy를 임의로
만들지 않았다. 운영 관측은 structured logging과 `processing_jobs.result_code` 집계로
가능하다. ingestion 이벤트를 감사 대상으로 삼을지는 결정이 필요하다.

### AN-2. `document_year` provenance 컬럼이 없다

추출기는 근거(`SINGLE_YEAR_IN_TITLE` / `AMBIGUOUS_MULTIPLE_YEARS` 등)를 계산하지만
저장할 곳이 없다. 운영 중 "이 연도는 어디서 왔나"를 물을 수 없고, 추출 규칙을 바꿔도
어떤 값이 옛 규칙으로 채워졌는지 구분할 수 없다.

지시대로 `document_year_source` 컬럼을 **추가하지 않았다.** 필요성이 확인되면 별도
스키마 변경으로 처리한다.

### AN-3. 긴 문단 분할의 문자 offset을 저장하지 않는다

한 문단이 여러 chunk로 쪼개지면 모두 같은 `paragraph_start = paragraph_end`를 갖는다.
문단 내 몇 번째 조각인지는 `chunk_index` 순서로만 알 수 있고 원문 문자 offset은 없다.

citation이 "문단 N"까지만 요구하므로(API Contract §5.4) 현재는 충분하다. 문단 내
위치까지 표시해야 한다면 컬럼 추가가 필요하다 — 지금은 추가하지 않았다.

### AN-4. `extracted_text`에 `[문단]` / `[표]` 마커가 포함된다

`build_normalized_text()`를 재사용했다. 이 함수는 구조 힌트로 마커를 넣는다.
`NORMALIZER_VERSION`으로 버전 관리되고 결정적이라는 장점이 크다고 판단해 두 번째 텍스트
표현을 만들지 않았다. 다만 이 컬럼은 pg_trgm lexical 검색 대상이므로(DB v2.5 §26)
마커가 매칭에 미치는 영향은 검색 서비스 구현 시 확인이 필요하다.
