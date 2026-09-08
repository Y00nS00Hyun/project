# 사내 문서 관리 시스템 — 기능 명세서 (v2.4)

> **DESIGN FREEZE v1 — IMPLEMENTATION DEFAULT** · 2026-09-08
>
> 이 문서와 [DB 스키마 v2.4](database-schema-v2.4.md)가 구현의 authoritative specification이다.
> Frozen/Open 범위와 변경 정책은 [Design Freeze v1](design-freeze-v1.md)을 따른다.
> [v2.3](functional-spec-v2.3.md)은 수정 없이 history/reference로 보존한다.
>
> **v2.3 대비 주요 변경**
> 1. Vector 기본 semantic 경로 + pg_trgm 별도 lexical 경로, automatic RRF 기본 OFF
> 2. Paragraph-aware / target·hard max 64 tokens (provisional) / overlap 0
> 3. 독립 table-aware OFF, parser의 row/column/cell/span은 parsed_structure에 보존
> 4. 로컬 multilingual-e5-small / 384d / query·passage prefix 및 provenance
> 5. 초기 exact cosine, 문서별 best chunk score 집계 후 ranking, HNSW 미도입
> 6. cosine threshold 단독 근거/refusal 판정 금지, DOCX/PDF citation 통합 검증 방향 명시
> 7. 사전 PoC 완료, 사내 corpus 검증은 production validation gate로 분리
>
> 근거: [Parser PoC](poc/hwp-poc-report.md), [Search PoC](poc/korean-search-poc-report.md),
> [Chunking + Embedding PoC](poc/chunking-embedding-poc-report.md).
> **IMPLEMENTATION DEFAULT ≠ FINAL PRODUCTION OPTIMUM**.
> 새 일반 PoC나 서비스 코드를 추가하는 문서 변경이 아니다.

> **v2.3 변경 이력 (v2.2 대비, 아래 항목은 이미 반영 완료)**
>
> 1. HWP/HWPX Parser PoC 결과 반영 (`docs/poc/hwp-poc-report.md`)
> 2. HWP/HWPX citation primary anchor를 `paragraph_index`로 확정
> 3. 전 포맷 공통 citation 폴백 체인을 제거하고 **format-aware citation**으로 변경
> 4. `parse_status`(worker 실행 상태)와 `parse_result_code`(파싱 결과 의미) 분리
> 5. downstream 처리 상태(embedding/summary/tagging)에 `SKIPPED` 추가
> 6. 검색 가능 조건(`is_ready`)에 `parse_result_code = TEXT_EXTRACTED` 추가
> 7. `processing_jobs`에 structured `result_code` 추가 (통계·집계용)
> 8. 배포용(distribution)·암호화 HWP의 본문 추출 불가 한계 명시
> 9. `section_title`을 optional enhancement로 하향 (citation 필수 요소 아님)
> 10. Worker memory / 최대 파일 크기 정책을 **사내 corpus 재측정 이후 결정**으로 변경
> 11. Open Issues disposition 표 추가 (§28)
> 12. 개발 우선순위·순서에서 HWP/HWPX PoC를 완료 상태로 표시
>
> v2.2 문서(`docs/functional_spec.MD`)는 설계 이력으로 그대로 보존한다.

## 0. 문서 목적

본 문서는 사내 공유폴더에 산재한 문서를 통합 검색하고, 문서 내용을 기반으로 질의응답하며, 향후 사내 자료를 근거로 정해진 양식의 보고서를 자동 작성할 수 있는 **사내 문서 관리·검색·RAG 시스템**의 기능 및 비기능 요구사항을 정의한다.

본 시스템의 1차 목표는 다음과 같다.

> **사용자가 현재 접근 권한을 가진 사내 문서를 정확히 찾고, 해당 문서에서 근거를 찾아 답변을 받으며, 답변의 근거를 문서의 특정 revision 및 원문 위치까지 추적할 수 있도록 한다.**

템플릿 기반 보고서 자동 생성은 이 검색·RAG·권한·출처 추적 기반이 안정화된 이후 구현하는 후속 기능으로 정의한다.

---

# 1. 선행 게이트

외부 서비스·개발 도구 사용 전 아래 항목을 사내 보안정책 및 관련 담당자와 확인한다.
로컬 embedding을 기본으로 하는 API Contract·DB Migration 준비는 외부 Embedding API 승인을 전제로 하지 않는다.

## 1.1 외부 AI 사용 가능 범위

다음 항목을 각각 구분하여 승인 여부를 확인한다.

* 사내 문서를 외부 LLM API로 전송 가능한지
* 사내 문서를 외부 Embedding API로 전송 가능한지
* Claude CLI / Claude Code 등 개발 도구에 사내 코드 또는 문서를 입력 가능한지
* 생성된 답변·요약·태그 등의 외부 처리 결과를 사내에 저장 가능한지

어느 한 구간이 제한될 경우 전체 시스템을 재설계하지 않고 해당 Provider만 사내 게이트웨이 또는 온프레미스 모델로 교체할 수 있도록 한다.
MVP embedding은 로컬 실행을 기본으로 하며 외부 embedding API를 호출하지 않는다.
외부 LLM·개발 도구의 사용 승인은 별도로 유지하고, 이번 Freeze가 그 승인을 대신하지 않는다.

## 1.2 Provider 분리 원칙

LLM과 Embedding 기능은 별도 인터페이스로 분리한다.

```text
LLMProvider
├─ generate_answer()
├─ summarize()
├─ generate_tags()
└─ generate_report_section()

EmbeddingProvider
├─ embed_documents()
└─ embed_query()
```

비즈니스 로직에서 특정 외부 SDK를 직접 호출하지 않는다.

---

# 2. 프로젝트 개요

| 항목       | 내용                                               |
| -------- | ------------------------------------------------ |
| 목적       | 사내 문서를 보기 쉽고 찾기 쉽게 정리하고, 문서 기반 질의응답 및 보고서 작성을 지원 |
| 주요 사용자   | 사내 임직원                                           |
| 원본 저장 위치 | 기존 사내 공유폴더                                       |
| 핵심 기술 방향 | ACL-aware Vector + 별도 pg_trgm Search + RAG + Provenance (RRF optional, 기본 OFF) |
| 백엔드      | FastAPI                                          |
| 프론트엔드    | React + TypeScript + Vite                        |
| 데이터베이스   | PostgreSQL + pgvector                            |
| 배포 환경    | 사내 VM, Docker Compose                            |
| AI 활용    | LLM 답변·요약·태깅·보고서 작성, Embedding                   |
| 향후 연동    | MCP                                              |

---

# 3. 시스템 원칙

## 3.1 공유폴더가 Source of Truth

원본 문서는 기존 사내 공유폴더에서 관리한다.

시스템은 원칙적으로 다음 작업을 수행하지 않는다.

* 원본 파일 수정
* 원본 파일 삭제
* 원본 파일 이동

시스템이 담당하는 기능은 다음으로 제한한다.

* 검색
* 열람
* 다운로드
* 문서 메타데이터 관리
* 태그 관리
* RAG
* 파생 결과물 생성

## 3.2 권한 선필터링

접근 권한이 없는 문서 또는 chunk가 다음 단계에 진입해서는 안 된다.

```text
검색
RAG Retrieval
LLM Context
보고서 생성 Retrieval
MCP 조회
```

검색 후 결과를 숨기는 방식은 허용하지 않는다.

## 3.3 Current Revision 원칙

일반 검색과 신규 RAG는 기본적으로 각 논리 문서의 **현재 revision만** 검색한다.

과거 revision은 다음 목적으로만 유지한다.

* 문서 변경 이력
* 과거 RAG 답변의 근거 재현
* 감사
* 필요 시 과거 버전 조회

## 3.4 근거 없는 생성 금지

LLM은 검색된 사내 자료에 없는 사실·수치·정책을 임의로 만들어내지 않는다.

근거가 부족하면 다음과 같이 처리한다.

```text
관련 문서에서 확인할 수 없습니다.
```

보고서 생성에서는:

```text
[확인 필요: 관련 자료를 찾지 못했습니다.]
```

등 명시적인 placeholder를 사용한다.

## 3.5 Retrieved Content는 Instruction이 아님

검색된 문서 내용은 LLM에게 제공되는 **데이터**이며 시스템 지시가 아니다.

문서 내부에 포함된 명령형 문구를 시스템 명령으로 실행하지 않는다.

---

# 4. 전체 아키텍처

```text
[Shared Folder / Source of Truth]
    ↓ File Sync (SHA-256 / 경로 / document 식별)
[documents / document_revisions]
    ↓ Parser (HWP/HWPX custom parser)
    ↓ Normalize
    ↓ Paragraph-aware Chunking (64 provisional / overlap 0)
    ↓ Local intfloat/multilingual-e5-small (passage: / 384d)
[chunks / VECTOR(384)]
    ↓ parse SUCCESS + TEXT_EXTRACTED + embedding SUCCESS
[READY → current_revision_id 승격]
    │
    ├─ Summary / Auto-tagging (별도 non-blocking pipeline, P2)
    │
[User Search / 서버 인증 Identity]
    ↓ ACL Filter (retrieval 이전)
    ↓ current READY revision + not deleted
    ├─ Vector exact cosine (기본 semantic route)
    │      ↓ 전체 eligible chunk의 document별 MAX(score)
    └─ pg_trgm (별도 lexical route)
           ↓ document 단위 ranking
    ↓ Optional RRF (기본 OFF; 활성화 시 document ranking 간 결합)
    ↓ 최종 Document Aggregation / 중복 없는 Search Results
    ├─ Search UI
    └─ RAG (근거 chunk 선택·context assembly·refusal은 §13 / §28.3)

후속 P3:
[ACL-aware Retrieval] → [Template Report Generator] → [Draft + Sources]
```

일반 검색에서는 **문서별 max score 집계를 chunk 후보 LIMIT보다 먼저 수행**한다.
위 마지막 Document Aggregation은 이미 집계한 문서 순위의 최종 중복 없는 반환을 뜻하며,
RRF 전에 raw chunk top-K를 자른 뒤 문서 dedup하는 의미가 아니다(§11.3).
RAG는 매 질문마다 ACL과 current READY 조건을 다시 적용한다.
기존 P2/P3 로드맵은 유지하며 이번 Freeze가 해당 기능 구현을 앞당기지 않는다.

---

# 5. 핵심 데이터 모델

전체 DB 스키마는 별도 `database-schema.md`에서 정의한다.

기능 명세 수준에서는 아래 구조를 기본으로 한다.

```text
users
departments

documents
document_revisions
chunks

document_permissions

tags
document_tags

processing_jobs

chat_sessions
chat_messages
chat_message_sources

favorites
recent_views

report_templates
generated_reports
generated_report_sources

audit_logs
```

## 5.1 Documents

`documents`는 논리적 문서를 의미한다.

주요 정보:

```text
id
title
current_revision_id
current_file_path
department_id
owner_id
is_deleted
missing_since
created_at
updated_at
```

## 5.2 Document Revisions

`document_revisions`는 문서가 특정 시점에 가지고 있던 콘텐츠 상태를 나타낸다.

```text
id
document_id
revision_no

content_hash
source_path_at_ingest
source_modified_at

parser_name
parser_version

parse_status
parse_result_code
embedding_status
summary_status
tagging_status

created_at
```

`parse_status`와 `parse_result_code`의 역할 구분은 §8.2에서 정의한다.

revision 생성 이후 해당 revision의 콘텐츠와 chunk는 덮어쓰지 않는다.

### Revision 보존 범위

최소한 다음 정보는 과거에도 재현 가능해야 한다.

* 추출된 본문
* 구조화된 파싱 결과 또는 해당 결과의 저장 위치
* chunk
* 원본 위치 정보
* 콘텐츠 hash
* parser/version
* embedding/model 정보

과거 원본 바이너리 파일 자체까지 보관할지는 별도 저장정책으로 결정한다.

---

# 6. 지원 문서 형식

## 6.1 MVP

* HWP 5.x
* HWPX
* DOCX
* 텍스트 기반 PDF

### HWP 5.x의 구조적 제한

HWP 5.x는 지원 대상이지만, 암호화 문서 및 배포용(distribution) 문서 등
**본문 추출이 원천적으로 불가능한 형태가 존재**한다.

배포용 문서는 본문이 암호화된 `ViewText` 스트림에 저장되며, 본 시스템은
콘텐츠 보호를 우회하거나 복호화하지 않는다.

이 경우 ingestion 실패로 시스템 전체를 중단하지 않고, 해당 문서를
구조화된 상태 코드(`parse_result_code = ENCRYPTED`)로 분류한다.

> PoC corpus에서 일부 HWP 파일이 이 형태로 관찰되었다.
> **현 시점에서 이 비율을 사내 문서 전체 분포로 일반화하지 않는다.**
> 실제 비율은 사내 corpus 재측정으로 확인한다.

## 6.2 후순위

* 스캔 PDF/HWP OCR
* XLSX 등 추가 포맷
* 이미지 문서
* 복잡한 멀티모달 문서

## 6.3 스캔 문서

OCR을 MVP에 포함하지 않는다.

텍스트 추출이 불가능한 스캔 문서는:

```text
parse_result_code = OCR_REQUIRED
```

로 분류한다.

이는 **parser 실패가 아니라 parser가 정상적으로 내린 판정**이므로
`parse_status`는 `SUCCESS`가 될 수 있다(§8.2).

판정이 불확실한 경우에는 `OCR_REQUIRED`로 단정하지 않고 경고로만 남긴다.

이때 revision은 정상 처리된다.

```text
parse_status      = SUCCESS
parse_result_code = TEXT_EXTRACTED
+ 경고 기록
```

parser 경고는 별도 상태값을 만들지 않고 `document_revisions.parsed_structure`에
파싱 결과와 함께 저장한다. 경고는 검색 가능 여부에 영향을 주지 않는다.

## 6.4 Parser 채택 조건

실제 Parser 라이브러리 채택 전 다음을 확인한다.

* 사내 문서 호환성
* 유지보수 상태
* 성능
* 라이선스
* 보안 및 컴플라이언스

HWP/HWPX는 공개 corpus에서 검증한 사내 custom parser를 구현 기본으로 채택했다(§6.5).
사내 문서 호환성 검증은 production validation gate로 남아 있다.
DOCX / PDF parser 선택과 citation 검증은 ingestion 구현 과정의 parser integration test에서 수행하며
별도 대형 PoC를 새로 만들지 않는다.

## 6.5 HWP/HWPX Parser PoC 결과 (완료)

전체 내용은 `docs/poc/hwp-poc-report.md`를 따른다.
본 절에는 **설계 결정에 영향을 준 검증된 사실만** 옮긴다.

결론: **PASS WITH LIMITATIONS**

### 검증 조건

사내 문서를 확보하지 못해 **공개 third-party corpus 98건**(HWP 16, HWPX 82)으로 측정했다.
따라서 아래 수치는 **사내 문서 호환성의 근거가 아니며**, 사내 corpus 재측정이 별도로 필요하다.

### 검증된 사실

| 항목 | 결과 |
| --- | --- |
| 본문 추출 | HWP/HWPX 모두 가능 |
| 문단 순서 | 원문 순서 유지 |
| 표 | row/column/cell 및 병합 정보 보존 가능 |
| 한글/숫자 | 257,689자에서 encoding 손상 0 |
| paragraph anchor | 성공 66건 중 66건 확보 |
| **page anchor** | **성공 66건 중 0건 — 확보 불가** |
| section_title | 성공 66건 중 6건(약 9%) |
| 결정성 | 66/66. 프로세스를 달리한 5회 반복에서도 canonical 결과 동일 |
| 실패 분류 | ENCRYPTED / OCR_REQUIRED / EMPTY_DOCUMENT를 구조화된 코드로 분류 가능 |
| Worker 격리 | 개별 문서 실패가 배치 전체를 중단시키지 않음 |

### page anchor를 확보할 수 없는 이유

HWP/HWPX는 **페이지 번호를 파일에 저장하지 않는다.** 페이지는 렌더링 시점의 결과다.

파일에 존재하는 것은 줄 배치 캐시이며, 실측 결과 해당 캐시의 페이지 경계 플래그가
전혀 설정되어 있지 않았다. 강제 쪽나눔 표시만 세면 자연 흐름에 의한 쪽넘김을 놓치므로
잘못된 페이지 번호가 된다.

따라서 **page number를 추정하여 만들어내지 않는다.**

### 이 결과를 다른 포맷에 일반화하지 않는다

```text
HWP/HWPX page anchor 불가

≠

모든 문서 format에서 page anchor 불가
```

DOCX는 paragraph 기반 anchor를 우선하고 PDF는 검증된 page 기반 anchor를 사용할 수 있다.
실제 anchor 확보·원문 위치 일치는 ingestion parser integration test에서 확인한다(§14).

---

# 7. File Sync

## 7.1 감지 대상

공유폴더에서 다음 변경을 감지한다.

* 신규
* 수정
* 이동
* 복사
* 삭제

## 7.2 처리 정책

| 상황                                   | 처리                         |
| ------------------------------------ | -------------------------- |
| 신규 경로 + 신규 hash                      | 새 document + revision 생성   |
| 동일 경로 + hash 동일                      | `last_seen_at`만 갱신         |
| 동일 경로 + hash 변경                      | 동일 document의 새 revision 생성 |
| 기존 경로 소멸 + 신규 경로 + 동일 hash + 명확한 1:1 | 이동으로 판단                    |
| 동일 hash가 여러 경로에 존재                   | duplicate 후보로 유지           |
| 일정 기간 파일 미발견                         | soft delete                |

`hash 동일 = 이동`으로 무조건 판단하지 않는다.

## 7.3 삭제 처리

첫 미발견 시 즉시 삭제하지 않는다.

```text
파일 미발견
→ missing_since 기록
→ grace period
→ 계속 미발견
→ is_deleted=true
```

삭제된 문서와 chunk는 검색 후보에서 즉시 제외한다.

---

# 8. Processing / Ingestion

## 8.1 기본 흐름

```text
Shared Folder → File Sync
→ Hash 계산 → document 식별 / revision 생성
→ Parser (HWP/HWPX custom parser)
→ Normalize
→ Paragraph-aware Chunking (target/hard max 64 tokens, provisional / overlap 0)
→ Local multilingual-e5-small (passage: / 384d)
→ VECTOR(384)
→ Search READY (§8.3의 세 조건 모두 충족)
→ current_revision_id 승격
```

이후 별도 처리:

```text
→ Summary
→ Auto Tagging
```

## 8.2 단계별 상태

**실행 상태(status)와 결과의 의미(result code)를 분리한다.**

PoC에서 `OCR_REQUIRED`, `EMPTY_DOCUMENT`, `ENCRYPTED` 같은 결과가 실제로 발생했는데,
이들은 worker의 실행 실패가 아니라 **parser가 정상적으로 내린 판정**이다.
하나의 status 값으로 두 개념을 표현하면 "worker가 죽은 것"과 "스캔 문서라고 정확히 판정한 것"을
구분할 수 없다.

### 8.2.1 실행 상태

각 revision에서 다음 상태를 별도로 관리한다.

```text
parse_status
embedding_status
summary_status
tagging_status
```

값:

```text
PENDING    아직 작업이 실행되지 않음
RUNNING    현재 실행 중
SUCCESS    정상적으로 분석하고 결과를 반환함
FAILED     예상하지 못한 parser/worker 실패
SKIPPED    선행 결과에 따라 실행하지 않기로 결정함
```

`SKIPPED`는 downstream 상태(`embedding_status`, `summary_status`, `tagging_status`)에만 사용한다.
`parse_status`에는 `SKIPPED`를 사용하지 않는다.

### 8.2.2 파싱 결과 코드

`parse_result_code`는 파싱 **결과의 의미**를 나타낸다.

```text
TEXT_EXTRACTED       검색 가능한 본문을 확보함
EMPTY_DOCUMENT       문서는 열렸으나 본문이 없음
OCR_REQUIRED         본문 텍스트가 없고 이미지 기반으로 확인됨
ENCRYPTED            암호화 / 배포용 문서로 본문 접근 불가
CORRUPT              컨테이너 또는 내부 구조 손상
UNSUPPORTED_FORMAT   지원하지 않는 형식
PARSE_FAILED         원인을 특정할 수 없는 실패
```

`NULL`은 아직 파싱 결과가 나오지 않은 상태를 의미한다.

원인을 확신할 수 없는 경우 더 구체적인 코드로 분류하지 않고 `PARSE_FAILED`로 처리한다.

### 8.2.3 상태 조합 예시

```text
정상 문서
parse_status      = SUCCESS
parse_result_code = TEXT_EXTRACTED
embedding_status  = PENDING → SUCCESS

스캔 문서
parse_status      = SUCCESS
parse_result_code = OCR_REQUIRED
embedding_status  = SKIPPED
summary_status    = SKIPPED
tagging_status    = SKIPPED

빈 문서
parse_status      = SUCCESS
parse_result_code = EMPTY_DOCUMENT
embedding_status  = SKIPPED

배포용 HWP
parse_status      = SUCCESS
parse_result_code = ENCRYPTED
embedding_status  = SKIPPED

worker 자체 장애
parse_status      = FAILED
parse_result_code = NULL 또는 PARSE_FAILED
```

본문을 확보하지 못한 revision에 대해 embedding을 실행하지 않는다.

## 8.3 검색 가능 조건

현재 revision이 다음 조건을 모두 만족하면 검색 가능하다.

```text
parse_status      = SUCCESS
AND
parse_result_code = TEXT_EXTRACTED
AND
embedding_status  = SUCCESS
```

`parse_status = SUCCESS`만으로는 충분하지 않다.
스캔·빈·암호화 문서도 parser는 정상적으로 판정하므로 `SUCCESS`가 될 수 있기 때문이다.

요약이나 태깅 실패는 검색 가능 여부에 영향을 주지 않는다.

## 8.4 Processing Jobs

`processing_jobs`에서 각 작업을 독립적으로 추적한다.

```text
PARSE
EMBED
SUMMARIZE
TAG
REINDEX
```

기록 항목:

* 상태
* **결과 코드 (`result_code`)**
* 재시도 횟수
* 시작 시각
* 종료 시각
* 에러 메시지
* worker 정보

### result_code와 error_message의 역할 분리

자유 문자열 `error_message`만으로는 실패 유형을 집계할 수 없다.

```text
result_code
→ 시스템이 이해하는 구조화된 결과. 운영 통계·실패 유형 집계·Dashboard·Retry 판단에 사용

error_message
→ 사람이 원인을 확인하기 위한 상세 설명
```

예:

```text
result_code   = CORRUPT
error_message = "File header is valid but BodyText stream cannot be decoded..."
```

`result_code`는 재시도 판단에도 사용한다.
예를 들어 `ENCRYPTED`나 `UNSUPPORTED_FORMAT`은 재시도해도 결과가 달라지지 않는다.

## 8.5 Idempotency

동일 revision을 재처리해도 중복 chunk 또는 중복 embedding이 생성되지 않아야 한다.

PoC에서 동일 파일에 대한 파싱 결과가 결정적임을 확인했다.
프로세스를 달리한 5회 반복에서도 canonical parsing 결과가 동일했으므로,
parser 버전이 고정되어 있으면 재처리 시 동일한 chunk 입력이 보장된다.

## 8.6 Chunking 정책 — 구현 기본값

[Chunking + Embedding PoC §13](poc/chunking-embedding-poc-report.md#13-recommendation)의
C2 provisional production candidate를 구현 기본값으로 채택한다.

```text
paragraph-aware
Target = hard max = 64 tokens (provisional)
Overlap = 0
Independent table-aware = OFF
```

**64 tokens는 짧은 synthetic corpus에서 얻은 provisional default이며 실제 사내 장문
문서의 최적값으로 검증되지 않았다. IMPLEMENTATION DEFAULT ≠ FINAL PRODUCTION OPTIMUM.**

1. 원문의 paragraph를 순서대로 누적하고, 가능한 한 paragraph 경계를 유지한다.
2. 단일 paragraph가 hard max보다 긴 경우에만 내부 분할한다. 조용히 뒤쪽을 버리지 않는다.
3. source paragraph anchor를 유지하며 `chunks.paragraph_start/end`에 inclusive 0-based 범위를 기록한다.
   PoC의 synthetic sentence index를 실제 parser의 paragraph index로 대체해 쓰지 않는다.
4. `chunking_version`을 기록한다. tokenizer·정규화·직렬화·분할 규칙과 parameter를 재현할 수 있어야 한다.
5. `token_count` 및 64-token 상한은 embedding 모델 tokenizer의 prefix/special-token 제외 길이다.
   실제 입력은 query/document prefix와 special tokens까지 모델 최대 512 이내인지 검사한다.
   **암묵적인 tokenizer truncation을 허용하지 않는다.** 초과 시 명시적으로 오류 처리한다.

### Table 정책

MVP 기본 retrieval에서는 표를 별도 독립 chunk type으로 강제하지 않는다.
일반 문단 누적 경로에 표의 text 표현을 전달하며, 표가 하나의 블록으로 들어오는 경우에도
같은 hard max 정책을 적용한다. 긴 표의 최적 분할과 병합 셀 처리는 §28.3에 남긴다.

Parser가 보존한 **row / column / cell / span**, 문단 위치 및 경고는 기존
`document_revisions.parsed_structure`에 유지한다. text 직렬화를 위해 원본 structured
representation을 덮어쓰거나 버리지 않는다. 독립 table-aware OFF는 표 구조 폐기를 뜻하지 않는다.
`chunks.content_type` 신규 컬럼과 전용 table schema는 추가하지 않는다.

Chunking 변경으로 기존 source chunk를 덮어쓰지 않는 정책은 DB v2.4 §13/§28을 따른다.

---

# 9. Parser / Model Version 추적

재현성과 재처리를 위해 결과 생성에 사용된 구성 정보를 기록한다.

## 9.1 Parser

```text
parser_name
parser_version
parsed_at
```

`parser_name`과 `parser_version`은 revision 생성 결과와 함께 **반드시 기록한다.**

Parser를 교체하거나 개선했을 때, 과거 revision이 어떤 parser로 만들어진 결과인지
확인할 수 없으면 그 revision의 파싱 결과를 재현할 수 없다.
PoC에서 동일 입력에 대한 파싱 결과가 결정적임을 확인했으므로,
parser 버전만 고정되면 과거 결과를 재현할 수 있다.

## 9.2 Embedding

MVP implementation default는 **로컬 `intfloat/multilingual-e5-small`, 384 dimensions**다.
PoC와 같은 revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`을 초기 기준으로 사용한다.
한국어에도 `query: ...` / `passage: ...` prefix를 적용하고 L2-normalized vector를 저장한다.
외부 API embedding은 기본 경로에 포함하지 않는다.

다음 provenance를 실제 성공한 embedding 결과와 함께 반드시 기록한다.

```text
embedding_provider
embedding_model
embedding_dimension
embedding_version
embedded_at
```

`embedding_provider`는 실제 로컬 provider 구현을, `embedding_version`은 실제 사용한 model revision을
식별해야 한다. 미실행 상태에서 모델 사용 이력을 성공한 것처럼 기록하지 않는다.

Embedding 모델 교체 시 기존 `SUCCESS` 상태를 그대로 신뢰하지 않고 대상 revision에 `REINDEX` job을 생성한다.
차원이 달라지면 기존 chunks 재임베딩과 DB dimension migration을 함께 계획한다.
같은 차원이어도 다른 model/revision의 vector를 같은 검색 공간에서 혼용하지 않는다.

## 9.3 LLM

요약·태깅·RAG·보고서 생성에는 가능한 범위에서 다음 정보를 기록한다.

```text
llm_provider
llm_model
prompt_version
processed_at
```

---

# 10. 인증 및 ACL

**우선순위: P1 / 실제 사내 문서 투입 전 필수**

## 10.1 인증

사내 SSO를 사용한다.

사용자 identity는 서버가 인증 토큰에서 획득한다.

클라이언트가 전달한 `user_id` 값을 권한 판정에 사용하지 않는다.

## 10.2 문서 권한

기본 모델:

```text
subject_type
- USER
- DEPARTMENT

permission
- READ
- WRITE
- ADMIN
```

## 10.3 검색 권한

검색은 다음 순서로 수행한다.

```text
사용자 Identity
→ 접근 가능한 documents 계산
→ current READY revision 계산 (not deleted 포함)
→ 해당 revision의 chunk만 Retrieval
```

권한 없는 문서에서 생성된 chunk는 lexical/vector 검색 후보 자체에서 제거한다.
MVP ACL은 DB v2.4 §11의 allow-only 모델을 유지하며, 명시적 DENY/override 및 precedence는 OPEN이다.

## 10.4 RAG 권한

RAG도 매 질문마다 현재 ACL을 다시 적용한다.

과거에 접근할 수 있었던 문서라도 현재 권한이 없으면 새로운 LLM context에 전달하지 않는다.

## 10.5 기존 채팅 권한

기존 채팅을 다시 조회할 때도 답변 source가 되는 revision의 현재 ACL을 확인한다.

답변이 현재 접근 불가능한 문서에 의존하는 경우 정책에 따라 다음과 같이 표시한다.

```text
이 답변에 사용된 일부 문서에 대한 현재 접근 권한이 없습니다.
```

민감 정보의 재노출 방지를 위해 해당 답변 본문을 숨길 수 있도록 설계한다.

---

# 11. 검색

**우선순위: P1**

## 11.1 검색 경로와 Optional Hybrid

```text
Vector Search  = 기본 semantic retrieval 경로
pg_trgm        = 별도 lexical search 경로
RRF            = optional fusion mechanism
Automatic RRF  = OFF
```

기본 요청에서 두 검색을 자동 병렬 실행하거나 결과를 항상 결합하지 않는다.
경로 선택의 API 표현은 API Contract v1에서 정의한다. 두 경로 모두 서버 identity로 계산한
ACL과 current READY revision 조건을 **retrieval 이전**에 적용한다.

[Search PoC](poc/korean-search-poc-report.md)와
[Chunking + Embedding PoC §12](poc/chunking-embedding-poc-report.md#12-rrf-recheck)에서
RRF의 명확한 gain이 확인되지 않았다. 후자의 RRF 재검증은 지표 우승 C4에서만 했으며,
구현 기본 C2에서도 반드시 악화된다는 뜻은 아니다.

기존 PoC RRF 구현은 삭제하지 않고 architecture option으로 유지한다.
향후 actual corpus 근거로 활성화할 경우 각 경로의 **document 단위 순위**를 결합하며,
chunk 수가 많은 문서가 중복 득점하지 않도록 한다. 활성화는 Design Freeze 변경 정책을 따른다.
MVP에서는 별도 reranker를 사용하지 않는다.

## 11.2 Current READY Revision 검색

기본 검색에서는 반드시 다음 조건을 모두 만족하는 chunk만 사용한다.

```text
documents.is_deleted = FALSE
AND document_revisions.id = documents.current_revision_id
AND document_revisions.is_ready = TRUE
AND chunks.document_revision_id = document_revisions.id
AND 현재 사용자에게 document READ 이상 권한 존재
```

`latest_revision_id`는 가장 최근 발견본이며 처리 완료 여부와 무관하다.
`current_revision_id`는 가장 최근 READY 본이므로 두 값을 혼동하지 않는다.
새 revision이 처리 중이거나 본문을 확보하지 못하면 기존 current READY 본을 계속 검색한다.
`current_ready_chunks` VIEW도 ACL 자체를 적용하지 않으므로 사용자 권한 조건을 추가해야 한다.
과거 revision은 일반 검색 및 신규 RAG에서 제외한다.

## 11.3 문서 단위 Aggregation

Vector는 chunk 단위 cosine을 계산하고 일반 검색 결과는 문서 단위로 반환한다.

```text
ACL + current READY 조건을 만족하는 전체 chunk
→ chunk cosine score
→ document score = MAX(해당 document의 chunk score)
→ document ranking (동점은 document_id ASC)
→ document 단위 LIMIT / pagination
→ optional document-level RRF (기본 OFF)
→ 중복 없는 Search Results
```

**chunk top-K를 먼저 잘라낸 뒤 document dedup하지 않는다.**
RRF를 켜더라도 vector 후보 목록의 문서 집계는 위 순서를 지킨다.
`best_match_snippet`은 점수를 만든 best chunk와 연결하고 revision/anchor를 추적한다.

예시:

```json
{
  "document_id": "...",
  "title": "2026년 사업계획서",
  "score": 0.91,
  "best_match_snippet": "...",
  "matched_chunks_count": 4
}
```

같은 문서는 한 번만 노출한다. 위 JSON은 개념 예시이며 최종 응답 계약과
`matched_chunks_count`의 매칭 판정은 API Contract에서 정의한다.
RAG의 근거 chunk Top-K 및 context assembly는 문서 검색 aggregation과 별도로 OPEN이다.

## 11.4 한국어 Lexical Search — pg_trgm

MVP lexical 구현 기본값은 **pg_trgm**이다.
PoC에서 오타·띄어쓰기·부분 문자열에 유효했고 vector는 semantic·morphology에 유효했다.
`simple` FTS AND는 한국어 기본 검색에 부적합하므로 primary ranking engine으로 가정하지 않는다.
`simple` FTS OR도 이를 대체하는 기본 엔진으로 채택하지 않는다.

Lexical 검색은 권한과 current READY 조건을 만족하는 문서의 제목·메타데이터 및
해당 revision의 본문을 대상으로 한다. 후보 생성·순위 계산 전에 동일한 필터를 적용한다.
기존 `chunks.search_vector`는 선택적 FTS 확장용으로 유지하되 기본 pg_trgm 경로는 사용하지 않는다.
PGroonga 등 다른 lexical engine의 도입 여부는 실제 corpus 근거가 생길 때 검토할 OPEN 항목이다.
새로운 일반 lexical PoC를 선행 과제로 추가하지 않는다.

## 11.5 Vector Search 방식

MVP 초기 구현은 **PostgreSQL + pgvector exact cosine search**다.
`multilingual-e5-small`의 384d query/document vector를 동일 모델·revision·prefix 규칙으로 생성한다.
문서 score는 §11.3의 best chunk score를 따른다.
HNSW는 현재 추가하지 않는다. 실제 chunk 규모와 검색 latency를 측정한 후,
RAM·Recall 비용까지 확인하여 도입 시점을 결정한다.

---

# 12. 검색 UI

**우선순위: P1**

## 12.1 검색창

자연어 및 키워드 검색을 모두 지원한다.

## 12.2 필터

폴더 트리 대신 다음 필터를 제공한다.

* 부서
* 연도
* 태그
* 문서 유형

필터는 chip 형태를 기본으로 한다.

ACL은 사용자가 조작할 수 있는 필터가 아니라 서버에서 강제되는 보안 조건이다.

## 12.3 검색 결과

표시 항목:

* 문서명
* 작성/관리 부서
* 문서 유형
* 날짜
* 태그
* 검색 snippet
* 마지막 수정 시각
* 최신 revision 여부

## 12.4 문서 상세

지원 기능:

* 메타데이터 확인
* 원문 다운로드
* 태그 확인
* 접근 가능한 범위 내 version/revision 이력 확인
* 해당 문서를 근거로 한 질문 시작

브라우저에서 HWP를 완전히 렌더링하는 기능은 제공하지 않는다.

---

# 13. RAG 챗봇

**우선순위: P1**

## 13.1 기본 흐름

```text
질문
→ 사용자 인증
→ ACL Filter
→ Current READY Revision Filter
→ Vector Retrieval (기본; pg_trgm은 별도 lexical 경로)
→ Optional Fusion (RRF 기본 OFF)
→ 근거 Chunk 선택 / Context Assembly (Top-K와 조합 정책 OPEN)
→ LLM
→ 답변
→ Source Validation
→ 출처 표시
```

## 13.2 근거 부족 처리

**Vector cosine threshold 하나만으로 근거 존재 여부 또는 RAG refusal을 결정하지 않는다.**
No-answer query도 높은 cosine을 가질 수 있다는 PoC 결과를 반영한다.
RAG refusal 정책·threshold 숫자는 RAG 구현 단계의 검증으로 결정하며 이번 Freeze에서는 정하지 않는다.
검색 결과가 존재하는 것과 질문에 답할 근거가 완전한 것은 다르다.

다음 경우 답변 생성을 제한한다.

* 검색 결과 없음
* 질문에 직접 대응하는 근거 없음
* 필요한 수치가 문서에 없음
* 정답 문서가 있으나 사용자 권한 없음

기본 응답:

> 관련 문서에서 확인할 수 없습니다.

## 13.3 프롬프트 인젝션 방어

Retrieved 문서는 명령어가 아닌 데이터로 취급한다.

RAG Generation 단계에서는 불필요한 write 권한 및 외부 action tool을 연결하지 않는다.

## 13.4 세션

다음 구조로 저장한다.

```text
chat_sessions
chat_messages
chat_message_sources
```

각 assistant 답변과 실제 근거 chunk를 별도로 연결한다.

---

# 14. 출처 및 Provenance

**우선순위: P1**

출처는 최소 다음 수준까지 추적한다.

```text
document_id
document_revision_id
chunk_id
```

과거 답변의 source는 `document_revision_id`에 고정하여 이후 원본이 변경되더라도 당시 답변이 어떤 revision을 사용했는지 확인 가능해야 한다.

## 14.1 Format-aware Citation

**모든 문서 형식에 동일한 citation 폴백 체인을 적용하지 않는다.**

v2.2에서는 전 포맷 공통으로

```text
section_title → page_number → paragraph_index → chunk_index
```

를 사용했으나, HWP/HWPX PoC 결과 이 포맷들에서는 `page_number`를 확보할 수 없음이
확인되었다(§6.5). 확보 불가능한 단계를 폴백 체인에 두면 실제로는 항상 건너뛰게 되어
정책이 실제 동작과 어긋난다.

따라서 포맷별로 anchor 정책을 분리한다.

| Format | Primary anchor | 추가 정보 | 상태 |
| --- | --- | --- | --- |
| HWP 5.x | `paragraph_index` | `section_title` (있을 때만) | **확정** |
| HWPX | `paragraph_index` | `section_title` (있을 때만) | **확정** |
| PDF | 검증된 `page_number` 사용 가능 | 검증된 원문 위치 정보 | ingestion parser integration test에서 확인 |
| DOCX | paragraph 기반 anchor 우선 | 명시적으로 확보한 section 정보 | ingestion parser integration test에서 확인 |

### HWP / HWPX

Primary anchor:

```text
paragraph_index
```

가능한 경우 추가 정보:

```text
section_title
```

표시 예:

```text
2026년 사업계획서.hwp > 문단 143
```

`section_title`이 존재하면:

```text
2026년 사업계획서.hwp > 사업 추진계획 > 문단 143
```

### DB 매핑

`paragraph_index`는 DB에서 chunk의 문단 범위로 표현한다.

```text
chunks.paragraph_start
chunks.paragraph_end
chunks.section_title
```

별도의 `paragraph_index` 컬럼을 새로 추가하지 않는다.
chunk는 문단 하나가 아니라 문단 범위를 담을 수 있기 때문이다.

### PDF / DOCX

DOCX는 paragraph 기반 anchor를 우선하고 DB의 `paragraph_start/end`에 매핑한다.
PDF는 parser가 원문과 일치하는 위치를 확인한 `page_number`를 사용할 수 있다.
실제 추출·citation mapping은 ingestion 구현의 **parser integration test**에서 검증한다.
별도 대형 PoC를 추가하지 않는다. 검증되지 않은 page anchor는 생성하지 않으며,
확보하지 못한 값은 NULL로 두고 검증된 위치 정보만 표시한다.
HWP/HWPX 결과를 그대로 다른 포맷에 일반화하지 않는다.

## 14.2 page_number 취급

`chunks.page_number` 컬럼은 nullable이므로 **제거하지 않는다.**
PDF 등 다른 포맷에서 확보 가능할 수 있기 때문이다.

다만:

> **HWP/HWPX에서 `page_number`가 일반적으로 확보된다고 가정하지 않는다.**

UI와 citation 문자열은 `page_number`가 없는 것을 정상 상태로 취급해야 하며,
`p.N` 표기에 의존하도록 설계하지 않는다.

## 14.3 section_title 정책

PoC에서 `section_title` 확보율은 약 9%였다.
문서가 개요/제목 스타일을 명시적으로 사용한 경우에만 확보되기 때문이며,
이는 parser 결함이 아니라 실제 문서 작성 관행의 반영이다.

따라서:

```text
section_title  = optional enhancement
paragraph_index = HWP/HWPX primary anchor
```

`section_title`을 citation의 필수 요소나 일반적인 성공 기준으로 간주하지 않는다.

> `section_title`은 parser가 명시적인 문서 구조를 통해 신뢰할 수 있게
> 확보한 경우에만 저장한다.
>
> 추정 또는 heuristic으로 section title을 만들어내지 않는다.

글꼴 크기·굵기 등 시각적 특성으로 제목을 추측하지 않는다.

---

# 15. 자동 요약 및 태깅

**우선순위: P2 / MVP 이후**

## 15.1 자동 요약

문서 ingestion과 독립적으로 실행한다.

요약 실패가 검색 실패로 이어지지 않는다.

## 15.2 자동 태깅

LLM을 이용하여 다음 등의 태그 후보를 생성할 수 있다.

* 부서
* 업무 유형
* 문서 유형
* 주요 키워드

## 15.3 태그 Provenance

```text
AUTO
MANUAL
SYSTEM
```

을 반드시 구분한다.

사용자가 입력한 MANUAL 태그를 자동 태깅이 임의로 삭제하거나 덮어쓰지 않는다.

---

# 16. Revision / Duplicate / Related Document

## 16.1 Duplicate

파일 콘텐츠 hash가 완전히 동일한 경우.

자동으로 동일 문서라고 확정하지 않고 복사본 가능성도 고려한다.

## 16.2 Revision

동일한 논리적 문서의 내용이 수정된 이력.

예:

```text
A.hwp
→ 내용 수정
→ 기존 document 유지
→ revision 2 생성
→ current_revision_id 변경
```

이전 revision 및 chunk는 보존한다.

## 16.3 Version Group

파일명 또는 업무상 동일 계열로 추정되는 여러 별도 document.

자동 병합하지 않고 후보만 제공한다.

## 16.4 Related Document

Embedding 등으로 내용이 유사한 별도 문서.

관련 문서 후보로만 표시하며 revision으로 자동 판단하지 않는다.

---

# 17. 즐겨찾기 / 최근 본 문서

**우선순위: P2**

로컬스토리지 대신 서버 DB에 저장한다.

지원 기능:

* 즐겨찾기 추가/삭제
* 최근 조회 문서
* 최근 다운로드 문서
* 필요 시 최근 질문한 문서

모든 항목은 현재 ACL 적용 후 노출한다.

---

# 18. MCP 서버

**우선순위: P2~P3**

기존 FastAPI 기능이 안정화된 이후 Backend API를 MCP tool로 노출한다.

## 18.1 1차 Read-only

```text
search_documents
get_document
get_document_chunks
get_recent_documents
```

## 18.2 2차 Write

```text
tag_document
reprocess_document
```

모든 MCP 요청에도 웹 UI와 동일한 인증·ACL 정책을 적용한다.

---

# 19. 사내 자료 기반 템플릿 보고서 자동 생성

**우선순위: P3 / MVP+2**

## 19.1 목적

사용자가 작성하려는 보고서의 종류와 요구사항을 자연어로 입력하면, 시스템이 등록된 양식을 선택하고 사용자가 접근 가능한 사내 문서를 검색하여 **해당 양식에 맞는 보고서 초안**을 자동 생성한다.

예:

> 2026년 8월 프로젝트 진행상황 보고서를 월간업무보고 양식으로 작성해줘. 지난달 대비 변경사항과 주요 이슈를 포함해줘.

또는:

> 이 출장결과보고서 양식으로 일본 출장 내용을 작성해줘.

## 19.2 기본 원칙

LLM이 보고서의 전체 레이아웃과 스타일을 자유롭게 생성하도록 하지 않는다.

다음 역할을 분리한다.

```text
LLM
→ 어떤 내용을 넣을지 생성

Template Renderer
→ 어디에 어떻게 배치할지 담당
```

## 19.3 처리 흐름

```text
사용자 요청
↓
Template 선택
↓
Template Planner
↓
필요 Section / Field 식별
↓
Section별 ACL-aware Retrieval
↓
Section Generator
↓
Source Validation
↓
Structured Result
↓
Template Renderer
↓
보고서 초안
↓
사용자 검토
```

## 19.4 Template Planner

템플릿의 섹션과 필드를 분석한다.

예:

```text
1. 사업 개요
2. 주요 실적
3. 전월 대비
4. 주요 이슈
5. 향후 계획
```

내부적으로 다음과 같은 구조를 정의할 수 있다.

```json
{
  "business_overview": {},
  "performance": {},
  "month_over_month": {},
  "issues": {},
  "next_plan": {}
}
```

## 19.5 Section별 Retrieval

보고서 전체를 한 번의 검색으로 처리하지 않는다.

예:

```text
주요 실적
→ 실적 자료 위주 Retrieval

전월 대비
→ 현재월 + 이전월 자료 Retrieval

향후 계획
→ 계획/일정 문서 Retrieval
```

각 section에 필요한 evidence를 독립적으로 수집한다.

## 19.6 권한

보고서 생성 Retrieval에도 기존 ACL 정책을 그대로 적용한다.

사용자가 볼 수 없는 문서는:

* 검색하지 않음
* LLM에 전달하지 않음
* 결과에 반영하지 않음

## 19.7 근거 부족 처리

자료가 없는 section을 LLM이 추측하여 채우지 않는다.

예:

```text
[확인 필요: 향후 계획에 관한 근거 문서를 찾지 못했습니다.]
```

또는 해당 field를 비워둔다.

## 19.8 Structured Generation

LLM 결과는 가능하면 자유형 문서가 아닌 JSON 등 구조화된 형태로 먼저 생성한다.

예:

```json
{
  "performance_summary": "...",
  "main_issues": [
    "..."
  ],
  "next_plan": null
}
```

서버에서 schema validation을 수행한 뒤 실제 template에 삽입한다.

## 19.9 Template Renderer

MVP+2의 초기 지원 형식:

* DOCX
* HWPX

필요 시 이후 PDF 출력 추가.

기존 binary HWP의 임의 템플릿을 완벽하게 편집·재생성하는 기능은 초기 범위에서 제외한다.

## 19.10 Template 관리

`report_templates`

주요 정보:

```text
id
name
description
file_format
template_path
template_version
field_schema
created_by
is_active
created_at
```

템플릿 버전이 변경되어도 기존 생성 보고서가 어떤 버전을 이용했는지 추적할 수 있어야 한다.

## 19.11 생성 보고서

`generated_reports`

주요 정보:

```text
id
template_id
template_version
user_id
title
request_text
status
output_path

llm_provider
llm_model
prompt_version

created_at
```

## 19.12 생성 근거

`generated_report_sources`

예:

```text
generated_report_id
section_key
document_revision_id
chunk_id
```

각 보고서 section이 어떤 내부 자료에 근거했는지 추적한다.

## 19.13 결과물 정책

AI가 생성한 보고서는 **초안**으로 취급한다.

기본 흐름:

```text
AI 생성
→ 사용자 검토
→ 수정
→ 다운로드
```

생성된 보고서를 시스템이 사내 공유폴더에 자동으로 저장하거나 기존 원본을 덮어쓰지 않는다.

## 19.14 일반 RAG Chat과 분리

RAG Chat:

```text
질문
→ Retrieval
→ 답변
```

Report Generation:

```text
요청
→ Template Planning
→ Section Retrieval
→ Generation
→ Validation
→ Rendering
→ 문서
```

두 기능은 검색 인프라는 공유하되 애플리케이션 파이프라인은 분리한다.

---

# 20. 제외 또는 후순위 기능

| 기능                   | 처리       |
| -------------------- | -------- |
| 좌측 폴더 트리             | 제외       |
| HWP 완전 브라우저 렌더링      | 제외       |
| PDF 자동 미리보기          | 후순위      |
| 원본 파일 웹 수정           | 제외       |
| 원본 파일 삭제/이동          | 제외       |
| 자동 OCR               | 후순위      |
| Reranker             | 필요성 확인 후 |
| Binary HWP 템플릿 완전 편집 | 후순위      |
| 자동 보고서 승인·배포         | 제외       |
| 보고서의 공유폴더 자동 반영      | 제외       |
| 배포용/암호화 HWP 복호화·우회    | 제외       |

---

# 21. 비기능 요구사항

## 21.1 보안

* SSO
* ACL
* 서버 측 identity 판정
* 문서 Retrieval 선필터링
* 외부 LLM 및 외부 Embedding API 사용 시 승인 (embedding 기본은 로컬)
* Prompt Injection 대응
* 민감 데이터 로그 정책

## 21.2 감사 로그

`audit_logs`에 주요 action을 기록한다.

예:

```text
DOCUMENT_VIEW
DOCUMENT_DOWNLOAD
SEARCH
TAG_UPDATE
PERMISSION_UPDATE
RAG_QUERY
REPORT_GENERATE
REPROCESS
```

기록:

* user
* action
* target
* timestamp
* 필요한 최소한의 metadata

검색어·LLM prompt 등 민감할 수 있는 내용의 원문 로깅 여부는 별도 보안정책을 따른다.

## 21.3 관측성

최소 모니터링 항목:

* File Sync 성공/실패
* Parse 성공률
* Embedding 성공률
* 처리 대기 job 수
* 평균 parse 시간
* 평균 embedding 시간
* 검색 latency
* RAG latency
* LLM API 오류율
* Report Generation 성공률

## 21.4 Worker 격리 및 리소스 정책

문서 parsing worker는 API 서버와 **프로세스 단위로 분리한다.**

문서 하나의 parser 오류가 FastAPI 서비스 전체 장애로 전파되지 않아야 한다.
PoC에서 개별 문서 실패가 배치 전체를 중단시키지 않음을 확인했다.

### Worker Resource

PoC에서 HWPX parser의 peak memory가 파일 크기 대비 크게 증가할 수 있음을 확인했다.
현재 측정에서는 대략 다음 수준이 관찰되었다.

```text
peak memory ≈ compressed file size × 5
```

**이 값을 절대적인 production 공식으로 사용하지 않는다.**
공개 corpus에서의 관찰값이며 문서 구성에 따라 크게 달라진다.

실제 사내 corpus 재측정 후 다음을 결정한다.

```text
- worker memory limit
- 최대 허용 파일 크기
- 동시 처리 개수
```

**현재 단계에서 임의의 최대 파일 크기 값을 정하지 않는다.**

파싱 시간은 파일 크기보다 표의 셀 개수에 더 민감했다는 점도 재측정 시 고려한다.

## 21.5 배포

초기:

```text
Docker Compose

postgres + pgvector
backend
worker
frontend/nginx
```

필요 시 향후 별도 Queue 및 Worker scaling을 검토한다.

---

# 22. HWP/HWPX PoC 성공 기준 및 결과 (완료)

PoC는 완료되었다. 상세 내용은 `docs/poc/hwp-poc-report.md`.

**결론: PASS WITH LIMITATIONS**

| 검증 항목 | 성공 기준 | 결과 |
| --- | --- | --- |
| 본문 | 정상 추출 | ✅ 충족 |
| 문단 순서 | 원문 순서 유지 | ✅ 충족 |
| 제목 | 가능한 수준에서 추출 | ⚠️ 약 9%만 확보 — optional로 하향 (§14.3) |
| 표 | 셀 내용 및 행·열 관계 보존 | ✅ 충족 (병합 span 포함) |
| 한글/숫자 | 깨짐 없음 | ✅ 충족 (257,689자, 손상 0) |
| 페이지 | 가능한 경우 위치 확보 | ❌ **확보 불가** — 포맷이 저장하지 않음 |
| 페이지 확보 실패 | paragraph index로 폴백 | ✅ `paragraph_index`를 primary anchor로 확정 |
| 대용량 파일 | Worker에서 정상 처리 | ✅ 충족. memory 정책은 재측정 후 결정 (§21.4) |
| 암호화 | ENCRYPTED 등 명확한 실패 분류 | ✅ 충족 (배포용 문서 포함) |
| 손상 | CORRUPT 등 명확한 실패 분류 | ✅ 충족 |
| 스캔 문서 | OCR_REQUIRED | ✅ 충족 (불확실한 경우는 경고 처리) |
| Idempotency | 재처리 시 중복 chunk 없음 | ✅ 파싱 결과 결정성 확인 (66/66, 프로세스 5회 반복 동일) |
| Parser 오류 | API 서버에는 영향 없음 | ✅ 충족 |

### 남은 검증

위 결과는 **공개 third-party corpus 98건** 기준이다.
**사내 corpus로 재측정하기 전까지 사내 문서 호환성은 검증되지 않은 상태다.**

특히 본문을 확보한 HWP 5.x 표본이 적었으므로, HWP의 표 처리는 사내 문서로
재확인이 필요하다.

### section_title

`section_title`은 optional이다.

제목 구조를 안정적으로 추출하지 못해도 **paragraph anchor로 출처 기능을 유지할 수 있다.**
page anchor는 HWP/HWPX에서 사용할 수 없다.

---

# 23. 검색/RAG 평가 체계

완료된 Search 및 Chunking + Embedding PoC의 synthetic 문서 20건 / 질의 30건을
구현 회귀 검증의 기준으로 유지한다. 실제 사내 성능을 대표하는 평가셋으로 주장하지 않는다.
사내 corpus 확보 후 대표 질의·정답을 마련해 production validation gate에서 재평가한다.

단순한 성공 예제만 만들지 않고 실패 모드를 포함한다.

## 23.1 테스트 질문 유형

* 정확한 문서명
* 일반 키워드
* 조사/어미 변화
* 띄어쓰기
* 오타
* 부서 필터
* 연도 필터
* 여러 문서에 걸친 질문
* 표 안의 숫자
* 근거가 없는 질문
* 접근 권한 없는 문서가 정답인 질문
* 과거 revision과 현재 revision의 값이 다른 질문

## 23.2 지표

```text
Search Recall@1 / Recall@5 / MRR / nDCG@5 / primary_at_1
Source Hit Rate
Answer Correctness
Citation Correctness
Refusal Correctness
ACL Leakage Rate
```

검색 PoC의 P@1 표기는 `primary_at_1`이며 일반적인 relevant precision@1과 다르다.
No-answer 질의는 제거하지 않고 recall을 null로 두어 별도로 분석한다.
RAG 지표는 아직 측정하지 않았으며 실제 구현 단계에서 검증한다.

**ACL Leakage Rate 목표는 0이며 release blocker로 취급한다.**

## 23.3 평가셋 확장

실사용 `chat_sessions` 및 검색 로그에서 반복적으로 발생하는 대표 질문을 추출하여 평가셋에 지속 추가한다.

---

# 24. 개발 우선순위

## P0 — 착수 전

* 외부 LLM 승인
* 외부 Embedding API를 사용할 경우 승인 (MVP 기본은 로컬, 외부 호출 없음)
* Claude CLI/Code 사용정책 확인

## P1 — MVP 핵심

* ~~HWP/HWPX PoC~~ → **완료** (§6.5, `docs/poc/hwp-poc-report.md`)
* ~~한국어 검색 PoC~~ → **완료** (`docs/poc/korean-search-poc-report.md`)
* ~~Chunking + Embedding PoC~~ → **완료** (`docs/poc/chunking-embedding-poc-report.md`)
* ~~기능명세/DB v2.4 및 DESIGN FREEZE v1~~ → **완료**
* API Contract v1 → DB Migration
* Core DB
* File Sync
* Revision
* Ingestion
* Current Revision Search
* SSO
* ACL
* Vector 기본 검색 + pg_trgm 별도 lexical 검색 (RRF optional / 기본 OFF)
* Search UI
* RAG
* Citation
* RAG 평가

## P2 — MVP 이후

* 자동요약
* 자동태깅
* Version Group 후보
* Related Document
* 즐겨찾기
* 최근 조회
* MCP Read-only

## P3 — MVP+2

* 템플릿 기반 보고서 자동 생성
* MCP Write
* OCR
* 고급 보고서 편집
* 필요 시 Reranker

---

# 25. 개발 순서

```text
완료: HWP/HWPX Parser PoC / Korean Search PoC / Chunking + Embedding PoC
완료: 기능명세 v2.4 / DB 스키마 v2.4 / DESIGN FREEZE v1

1. API Contract v1
2. DB Migration (v2.4 기준; 이번 문서 작업에서는 구현하지 않음)
3. Core Backend / SSO + ACL (실제 사내 데이터 투입 전 적용)
4. File Sync / Revision
5. Ingestion
   ├─ 기존 HWP/HWPX custom parser 재사용
   ├─ DOCX/PDF parser integration test 및 citation 확인
   ├─ Normalize / Paragraph-aware 64 provisional / overlap 0
   ├─ local multilingual-e5-small / VECTOR(384)
   └─ Idempotency / provenance / READY 승격
6. ACL-aware Search Backend
   ├─ ACL → current READY revision → retrieval
   ├─ exact cosine 기본 / pg_trgm 별도 lexical
   ├─ 문서별 MAX(score) → document ranking
   └─ optional RRF 기본 OFF
7. Search UI
8. RAG / Citation / Refusal / Prompt Injection / Chat History ACL / 평가
9. 기존 P2/P3 로드맵 (§24): 부가기능 → MCP → 템플릿 보고서 생성

독립 production validation gate (데이터 확보 후, 실제 사내 운영 승인 전):
   HWP/HWPX 재측정 / Search 재평가 / Chunking·Embedding 재평가
```

새로운 일반 PoC를 추가하지 않는다. 사내 corpus 미확보는 API Contract·DB Migration·
서비스 구현을 시작하지 못하게 하는 architecture blocker가 아니다.
실제 사내 운영 승인은 validation gate 결과를 별도로 확인해야 하며, 검증을 통과한 것으로 간주하지 않는다.
외부 서비스 및 개발 도구 사용은 §1의 보안정책을 따른다.

---

# 26. MVP 완료 기준

다음 조건을 모두 충족하면 1차 MVP 완료로 판단한다.

1. 공유폴더의 신규·수정·이동·삭제 파일이 정상 동기화된다.
2. HWP/HWPX/DOCX/PDF의 텍스트를 검색 가능한 수준으로 추출한다.
   본문을 확보하지 못한 문서(암호화·스캔·빈 문서)는 실패로 방치하지 않고
   구조화된 `parse_result_code`로 분류한다.
3. 변경된 문서는 새로운 revision으로 관리되고 기존 revision은 보존된다.
4. 검색은 `current_revision_id`가 가리키는 가장 최근 READY revision만 사용한다.
   처리 중인 `latest_revision_id`를 검색 대상으로 승격하지 않는다.
5. 권한 없는 문서는 검색 결과에 노출되지 않는다.
6. 권한 없는 chunk가 LLM에 전달되지 않는다.
7. 한국어 키워드 검색과 의미 기반 검색이 동작한다.
8. 같은 문서의 여러 chunk가 일반 검색 결과에 중복 노출되지 않는다.
9. RAG가 근거 문서를 바탕으로 답변한다.
10. 답변에서 사용한 revision 및 원문 위치를 추적할 수 있다.
    HWP/HWPX에서는 `paragraph_index`가 원문 위치의 기준이다(§14.1).
11. 근거가 없는 질문에서는 답변을 추측하지 않는다.
12. ACL Leakage Rate가 0이다.
13. parser/embedding/LLM 처리 실패를 운영자가 추적할 수 있다.
    실패 유형은 자유 문자열이 아니라 구조화된 result code로 집계할 수 있다.
14. 문서 처리 Worker 오류가 API 서버 장애로 이어지지 않는다.

**MVP의 최종 정의**

> 사용자가 현재 열람 권한을 가진 사내 문서를 정확하게 찾고, 해당 문서를 근거로 질문하며, 답변의 근거가 어느 문서의 어느 revision 및 어느 위치에서 나왔는지 확인할 수 있는 상태.

---

# 27. 템플릿 보고서 생성 완료 기준

P3 기능은 다음 조건을 만족하면 1차 완료로 본다.

1. 사용자가 보고서 종류와 작성 요청을 자연어로 입력할 수 있다.
2. 등록된 DOCX/HWPX 템플릿을 선택할 수 있다.
3. 템플릿의 section별로 필요한 자료를 검색한다.
4. 검색에는 현재 ACL이 적용된다.
5. 보고서 각 section의 생성 근거를 revision/chunk 수준으로 저장한다.
6. 자료가 없는 항목을 임의로 생성하지 않는다.
7. LLM 출력은 구조화된 schema validation을 통과한 뒤 template에 삽입한다.
8. 원본 template의 주요 레이아웃과 서식을 유지한다.
9. 결과를 초안 상태로 사용자에게 제공한다.
10. 자동으로 사내 원본 공유폴더를 수정하지 않는다.
11. 어떤 template version, LLM model, prompt version으로 생성했는지 추적할 수 있다.
12. 사용자가 최종 검토 후 결과물을 다운로드할 수 있다.

---

# 28. Open Issues / Known Limitations

§28.1은 HWP/HWPX Parser PoC 당시 이슈의 v2.3 반영 이력이다.
FIX NOW 항목은 v2.3에서 이미 반영했으며 v2.4에서 해당 구조를 다시 변경하지 않는다.
현재 Freeze 이후 OPEN 및 validation gate는 §28.3과 `docs/design-freeze-v1.md`를 따른다.

분류 기준:

```text
FIX NOW   현재 기능 명세 / DB에 반영해야 함
ACCEPT    현재 기술적 한계를 시스템 설계로 받아들임
DEFER     MVP 이후 해결
```

## 28.1 Disposition

| ID | Issue | Decision | Reason | Affected Document |
| --- | --- | --- | --- | --- |
| OI-1 | `OCR_REQUIRED`를 저장할 상태값이 스키마에 없음 (기능명세는 요구, DB `parse_status` CHECK는 4값만 허용) | **FIX NOW** | 명세와 스키마가 직접 충돌. `parse_status`(실행 상태)와 `parse_result_code`(결과 의미)를 분리하여 해소 | 기능명세 §8.2 / DB `document_revisions` |
| OI-2 | 구조화된 실패·결과 코드 컬럼 없음 (`processing_jobs.error_message` 자유 문자열뿐) | **FIX NOW** | 실패 유형 집계가 불가능하여 §21.3 관측성 및 §26-13 운영 추적 요구를 충족할 수 없음 | 기능명세 §8.4 / DB `processing_jobs.result_code` |
| OI-3 | `page_number` 기반 출처 표기가 HWP/HWPX에서 실현 불가 | **FIX NOW** | 전 포맷 공통 폴백 체인이 실제 동작과 어긋남. format-aware citation으로 변경 | 기능명세 §14 / DB `chunks.page_number` 주석 |
| OI-4 | 정규화(normalizer) 버전을 기록할 컬럼 없음 | **DEFER** | 전용 normalizer_version 컬럼은 추가하지 않는다. 현재는 정규화·직렬화·분할 규칙을 chunking_version으로 재현하고, 독립 normalizer 버전 관리의 필요성은 운영 근거가 생길 때 검토한다 | DB `document_revisions` (MVP 이후) |
| OI-5 | 배포용(distribution) HWP 처리 정책 부재 | **FIX NOW** (일부 DEFER) | 본문 추출 불가라는 기술적 사실과 상태 코드 분류는 지금 명시한다(§6.1). 사내 배포용 문서 비율 실측과 원본 재수급 프로세스는 사내 corpus 재측정 이후 결정 | 기능명세 §6.1 / §20 |
| OI-6 | 확장자와 실제 포맷 불일치 파일(`.hwp`인데 실제로는 HWPX) 처리 위치 미정 | **DEFER** | `parse_result_code = UNSUPPORTED_FORMAT`으로 안전하게 분류되므로 데이터 무결성 위험은 없다. 시그니처 기반 자동 재라우팅을 File Sync에 둘지는 File Sync 구현 단계에서 결정 | 기능명세 §7 (MVP 이후) |

## 28.2 Known Limitations

설계로 해결하지 않고 한계로 수용한 사항이다.

### KL-1. HWP/HWPX page anchor 확보 불가

포맷이 페이지 번호를 저장하지 않는다. parser 개선으로 해결되지 않는다.
페이지 번호가 반드시 필요해지는 요구는 렌더링·변환·매핑을 수반하는 별도 architecture 변경 사안이다.
이번 Freeze에는 포함하지 않으며 새로운 일반 PoC를 선행 과제로 추가하지 않는다.

### KL-2. 배포용·암호화 HWP는 본문 ingestion 불가

`parse_result_code = ENCRYPTED`로 분류한다. 복호화·우회는 범위에서 제외한다(§20).
해당 문서는 검색 대상이 되지 않는다.

### KL-3. section_title 확보율이 낮음

약 9%. 추정으로 만들어내지 않는다는 원칙의 직접적 결과다(§14.3).

### KL-4. 사내 문서 호환성 미검증

Parser PoC는 공개 third-party corpus, Search/Chunking + Embedding PoC는 synthetic corpus 기준이다.
HWP/HWPX custom parser와 검색·chunking·embedding의 **구현 기본값은 Freeze**한다.
실제 사내 문서의 호환성과 성능은 여전히 미검증이며 corpus 확보 후 production validation gate를 통과해야 한다.
데이터 미확보로 API Contract·DB Migration·구현 착수를 다시 일반 PoC 단계로 돌리지 않는다.

### KL-5. 중첩 표·머리말/꼬리말·각주·수식 미수집

PoC 파서는 본문 흐름, 도형 내 텍스트, 표 셀만 수집한다.
중첩 표는 평탄화되며 부모-자식 관계가 보존되지 않는다.
이들 요소에 실질 정보가 담긴 문서가 많다면 사내 corpus 재측정 시 별도 판단한다.

## 28.3 Open Decisions / Production Validation Gates

다음은 의도적으로 OPEN이다. 이 목록이 핵심 architecture 전체의 미확정을 뜻하지 않는다.

| 항목 | 결정 또는 검증 시점 |
| --- | --- |
| 실제 사내 corpus에서 search 성능 | corpus 확보 후 Search production validation gate |
| 실제 사내 장문용 최적 chunk size | 64 provisional 기본값으로 구현 후 actual corpus에서 검증 |
| 실제 긴 표 / 병합 셀 처리 | parsed_structure 보존 상태에서 parser·chunking 통합 검증 |
| HNSW 적용 시점 | 실제 chunk 규모 / 검색 latency / RAM / Recall 측정 후 |
| RAG retrieval Top-K | RAG 구현·근거 검증 단계 |
| RAG context assembly | 인접 문단·표 header·값의 근거 완전성 검증 후 |
| RAG refusal threshold | no-answer 포함 RAG 검증 후; cosine threshold 단독 판단 금지 |
| PGroonga 최종 도입 여부 / RRF 활성화 | actual corpus에서 현재 기본값보다 유효하다는 근거가 생길 때 |
| ACL DENY precedence / override | 실제 보안 요구와 권한 충돌 규칙 확정 후; MVP allow-only 유지 |
| worker production memory / concurrency / 최대 파일 크기 | 실제 사내 문서 구성과 동시 처리 부하 측정 후 |

기존 후속 운영 결정인 retry backoff, File Sync missing grace period, extracted content의
Object Storage 이전 시점도 유지한다(DB v2.4 §32). 새 스키마나 수치를 선제 추가하지 않는다.

Validation gate는 **데이터 확보 후 실제 사내 운영 승인 전** HWP/HWPX 재측정,
Search 재평가, Chunking/Embedding 재평가다. 현재 상태는 **미검증 / 데이터 대기**다.
DOCX/PDF는 ingestion parser integration test로 확인한다. 자세한 통과 범위와 변경 정책은
[Design Freeze v1](design-freeze-v1.md)을 따른다.
