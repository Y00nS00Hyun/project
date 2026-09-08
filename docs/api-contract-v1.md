# 사내 문서 관리 시스템 — API Contract v1

작성일: 2026-09-08 · **API Contract: READY** · **Consistency Check: PASS**

구현 기준은 [기능명세 v2.4](functional-spec-v2.4.md), [DB 스키마 v2.4](database-schema-v2.4.md),
[Design Freeze v1](design-freeze-v1.md)이다. 이 문서는 **구현 전 계약**이며 세 문서를 수정하지 않는다.

> **범위**: React frontend ↔ FastAPI backend가 동일한 request / response / error /
> pagination / permission 규칙을 쓰도록 MVP 최소 API를 정의한다.
> 이번 단계에서 API 코드·Pydantic 모델·migration·OpenAPI 생성물을 만들지 않는다.

---

## 1. 공통 규칙

| 항목 | 값 |
| --- | --- |
| Prefix | `/api/v1` |
| Content-Type | `application/json; charset=utf-8` (다운로드만 binary) |
| ID | UUID v4 문자열. 예외: `tags.id`는 스키마상 `SERIAL`이므로 **정수** |
| 시각 | ISO 8601, **UTC**, `Z` 접미사 (`2026-09-08T02:31:00Z`) |
| 문자 인코딩 | UTF-8 |
| 요청 추적 | 응답 헤더 `X-Request-Id`, 오류 본문 `error.request_id`와 동일 값 |

백엔드 내부 기준 시각은 UTC이고 DB는 `TIMESTAMPTZ`를 사용한다.
표시용 로컬 타임존 변환은 frontend 책임이다.

`tags.id`가 정수인 것은 DB v2.4 `CREATE TABLE tags (id SERIAL PRIMARY KEY ...)`를 따른 것이며,
"모든 ID는 UUID" 규칙의 유일한 예외다. 계약에서 임의로 UUID로 바꾸지 않는다.

---

## 2. 인증 / 인가

### 2.1 인증

사내 SSO를 사용한다. 모든 `/api/v1` endpoint는 인증을 요구한다(공개 endpoint 없음).

**사용자 identity는 서버가 인증 토큰에서 얻는다.**
요청 body·query·header로 전달된 `user_id`류 값을 권한 판정에 사용하지 않는다(기능명세 §10.1).
그런 필드는 이 계약 어디에도 정의하지 않는다.

인증 실패 시 `401 UNAUTHENTICATED`.

### 2.2 인가 (ACL)

Document를 다루는 모든 경로에서 다음 순서를 강제한다.

```text
서버 identity
→ 접근 가능한 document 집합 계산
→ current READY revision 계산 (is_deleted = FALSE 포함)
→ 그 revision의 chunk만 retrieval
```

**권한 없는 chunk를 검색한 뒤 결과에서 제거하는 구조를 허용하지 않는다**(기능명세 §10.3).
RAG도 동일하며, 권한 없는 chunk는 LLM context에 진입하지 않는다(§10.4).

MVP ACL은 allow-only다(`READ` < `WRITE` < `ADMIN`, 사용자/부서 권한 중 최대값).
명시적 DENY/override와 precedence는 Design Freeze의 OPEN 항목이므로 이 계약에 노출하지 않는다.

---

## 3. 오류 계약

### 3.1 스키마

모든 JSON 오류는 이 구조를 사용한다.

```json
{
  "error": {
    "code": "DOCUMENT_NOT_FOUND",
    "message": "문서를 찾을 수 없습니다.",
    "request_id": "01JB8Q2K7M3N4P5Q6R7S8T9V0W"
  }
}
```

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `code` | string | 아래 closed set의 안정적 식별자. frontend 분기는 이 값으로만 한다 |
| `message` | string | 사용자에게 보여줄 한국어 문장 |
| `request_id` | string | 로그 상관관계용. `X-Request-Id`와 동일 |

검증 오류는 `error.details`(선택)에 필드 단위 정보를 담을 수 있다.

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "요청 값이 올바르지 않습니다.",
    "request_id": "...",
    "details": [
      { "field": "size", "reason": "1 이상 100 이하여야 합니다." }
    ]
  }
}
```

**스택 트레이스, 예외 클래스명, SQL, 공유폴더 경로, 내부 호스트명을 응답에 넣지 않는다.**
이 값들은 서버 로그에만 남기고 사용자에게는 `request_id`로 연결한다.

### 3.2 Status 정책

| Status | 사용 시점 |
| --- | --- |
| `200 OK` | 조회·수정 성공 |
| `201 Created` | 리소스 생성 성공 (`POST /chat/sessions`, `POST .../messages`) |
| `400 BAD_REQUEST` | 형식은 맞으나 의미가 잘못된 요청 |
| `401 UNAUTHENTICATED` | 인증 없음/만료 |
| `403 FORBIDDEN` | **존재를 이미 아는 리소스**에 대한 권한 부족 |
| `404 NOT_FOUND` | 없거나, 존재를 숨겨야 하는 경우 |
| `409 CONFLICT` | 상태 충돌 |
| `422 VALIDATION_ERROR` | 스키마/타입/범위 위반 |
| `500 INTERNAL_ERROR` | 서버 오류 |

### 3.3 403 과 404 의 구분 — ACL 정책과의 정합

기능명세 §10.3은 권한 없는 문서를 **후보 자체에서 제외**하라고 요구한다.
따라서 사용자 관점에서 그 문서는 "권한이 없는 문서"가 아니라 **"존재하지 않는 문서"** 여야 한다.
403을 주면 문서의 존재·ID 유효성이 노출되어 검색 선필터링으로 감춘 정보가 오류 코드로 새어나간다.

규칙:

```text
Document / Revision / Download / 타인의 Chat Session
  → 접근 권한 없음 = 404 NOT_FOUND   (존재 여부를 숨긴다)

이미 접근 가능한 리소스에 대해 허용되지 않는 동작
  → 403 FORBIDDEN
```

`403`은 v1 범위에서 **Chat Session 쓰기 권한**에만 실질적으로 발생한다.
Document 계열은 v1에 쓰기 동작이 없으므로 403을 사용하지 않는다.

### 3.4 오류 코드 (closed set)

| code | status | 발생 위치 |
| --- | --- | --- |
| `UNAUTHENTICATED` | 401 | 전역 |
| `FORBIDDEN` | 403 | Chat |
| `VALIDATION_ERROR` | 422 | 전역 |
| `BAD_REQUEST` | 400 | 전역 |
| `DOCUMENT_NOT_FOUND` | 404 | Documents / Download |
| `REVISION_NOT_FOUND` | 404 | Revisions / Download |
| `DOCUMENT_NOT_DOWNLOADABLE` | 409 | Download (원본 파일 접근 불가) |
| `CHAT_SESSION_NOT_FOUND` | 404 | Chat |
| `CHAT_MESSAGE_TOO_LONG` | 422 | Chat |
| `SEARCH_QUERY_TOO_LONG` | 422 | Search |
| `RATE_LIMITED` | 429 | 전역 (적용 시) |
| `INTERNAL_ERROR` | 500 | 전역 |

새 코드를 추가할 때는 이 표를 갱신한다. frontend는 표에 없는 코드를 만나면
`error.message`를 그대로 노출하고 일반 오류로 처리한다.

---

## 4. Pagination

목록 API는 `page` / `size` 방식을 사용한다.

| 파라미터 | 기본 | 범위 |
| --- | --- | --- |
| `page` | `1` | `>= 1` |
| `size` | `20` | `1 ~ 100` |

범위를 벗어나면 조용히 보정하지 않고 `422 VALIDATION_ERROR`를 반환한다.

응답 봉투:

```json
{
  "items": [],
  "page": 1,
  "size": 20,
  "total": 0
}
```

`total`은 **문서(또는 리소스) 단위 총 개수**이며 chunk 개수가 아니다.
정렬은 항상 결정적이어야 하며, 동점 시 `document_id ASC`(또는 해당 리소스의 id ASC)로 고정한다.
그렇지 않으면 페이지 경계에서 항목이 중복·누락된다.

---

## 5. 공통 객체

### 5.1 `Department`

```json
{ "id": "6b1f...", "name": "기획조정실" }
```

### 5.2 `Tag`

```json
{ "id": 12, "name": "보안" }
```

### 5.3 `RevisionRef`

문서 요약 응답에 들어가는 최소 revision 참조다.

```json
{
  "revision_id": "9a3c...",
  "revision_no": 4,
  "created_at": "2026-08-30T04:12:00Z"
}
```

### 5.4 `Anchor` — 출처 위치 (discriminated union)

`type`으로 구분한다. **없는 값을 추측해 만들지 않는다.**

포맷별 규칙은 기능명세 §14.1을 따른다.

```json
{ "type": "paragraph", "paragraph_index": 42, "paragraph_end": 43 }
```

```json
{ "type": "page", "page_number": 7 }
```

```json
{ "type": "none" }
```

| type | 필드 | 사용 포맷 |
| --- | --- | --- |
| `paragraph` | `paragraph_index` (필수), `paragraph_end` (선택) | HWP / HWPX / DOCX |
| `page` | `page_number` (필수, `>= 1`) | PDF (parser가 원문 일치를 검증한 경우만) |
| `none` | 없음 | 위치를 신뢰할 수 없을 때 |

**DB 매핑**: `paragraph_index` = `chunks.paragraph_start`,
`paragraph_end` = `chunks.paragraph_end`(단일 문단이면 생략).
기능명세 §14.1이 정의한 `paragraph_index ↔ paragraph_start/paragraph_end` 매핑을 그대로 쓴다.
새 컬럼을 요구하지 않는다.

`section_title`은 optional enhancement이므로 `Anchor`가 아니라 출처 객체의 별도 선택 필드로 둔다.
값이 없으면 필드를 생략하거나 `null`로 두며, heuristic으로 만들지 않는다.

**`page_number`가 없는 것은 오류가 아니다.** HWP/HWPX에서는 정상 상태다.
frontend는 `type`으로 분기해야 하며 `page_number` 존재를 가정하지 않는다.

### 5.5 `DocumentSummary`

검색 결과와 목록에서 공통으로 쓰는 문서 요약이다.

```json
{
  "document_id": "3f2a...",
  "title": "2026년 AI 문서관리 사업계획서",
  "file_type": "hwp",
  "department": { "id": "6b1f...", "name": "기획조정실" },
  "tags": [{ "id": 12, "name": "보안" }],
  "updated_at": "2026-08-30T04:12:00Z",
  "current_revision": {
    "revision_id": "9a3c...",
    "revision_no": 4,
    "created_at": "2026-08-30T04:12:00Z"
  },
  "has_newer_revision": false
}
```

| 필드 | 출처 |
| --- | --- |
| `file_type` | `documents.file_type`. **소문자** `hwp` / `hwpx` / `docx` / `pdf` (DB CHECK와 동일) |
| `department` | `documents.department_id` → `departments`. 미지정이면 `null` |
| `tags` | `document_tags` (MANUAL/SYSTEM). AUTO 태그는 §12 참조 |
| `updated_at` | `documents.updated_at` |
| `current_revision` | `documents.current_revision_id` |
| `has_newer_revision` | `latest_revision_id <> current_revision_id`. 기능명세 §12.3 "최신 revision 여부" |

`file_type`을 대문자로 바꾸지 않는다. DB 값과 API 값이 달라지면 양쪽에 변환 규칙이 생겨
불일치의 원인이 된다.

`has_newer_revision = true`는 "더 최신 파일이 발견됐지만 아직 처리 중"이라는 뜻이며,
검색 결과가 오래됐다는 신호가 아니다. 검색은 언제나 current READY revision을 사용한다.

**`source_path` / `original_filename` / `parsed_structure` / `extracted_text`는 응답에 포함하지 않는다.**
공유폴더 경로 노출 금지(기능명세 §3.1, §13 다운로드 규칙)와 응답 크기 때문이다.

---

## 6. Search API

```http
GET /api/v1/search
```

### 6.1 Query parameters

| 이름 | 타입 | 필수 | 기본 | 설명 |
| --- | --- | --- | --- | --- |
| `q` | string | 아니오 | — | 검색어. 최대 512자 |
| `page` | int | 아니오 | `1` | |
| `size` | int | 아니오 | `20` | `1~100` |
| `department_id` | UUID | 아니오 | — | `documents.department_id` 일치 |
| `tag_id` | int | 아니오 | — | 반복 지정 시 **AND** (모두 가진 문서) |
| `file_type` | string | 아니오 | — | `hwp`/`hwpx`/`docx`/`pdf` |
| `year` | int | 아니오 | — | **OI-1 참조.** 근거 필드 미확정 |
| `mode` | enum | 아니오 | `default` | §6.3 |

모든 filter는 선택이며 서로 AND로 결합한다.
정의되지 않은 query parameter는 무시하지 않고 `422 VALIDATION_ERROR`로 거절한다
(오타난 filter가 조용히 무시되어 "권한이 걸린 줄 알았던" 결과가 나오는 것을 막는다).

### 6.2 `q`가 없을 때

`q`가 없거나 공백만 있으면 **검색이 아니라 필터 브라우징**으로 동작한다.

```text
q 있음  → ACL + current READY 필터 → retrieval → 관련도 순
q 없음  → ACL + current READY 필터 → updated_at DESC, document_id ASC
```

`q`가 없으면 retrieval을 수행하지 않으므로 응답의 `snippet`과 `matched_chunk`는 `null`이다.
이는 기능명세 §12.2의 chip 필터 UI(부서/연도/태그/문서유형만 선택한 상태)를 지원하기 위한 것이다.
`q`도 없고 filter도 없으면 전체 접근 가능 문서를 `updated_at DESC`로 반환한다(오류 아님).

### 6.3 `mode`

Design Freeze v1은 Vector를 기본 semantic 경로로, pg_trgm을 **별도 lexical 경로**로 두고
automatic RRF는 OFF다. lexical 경로에 도달할 방법이 없으면 그 경로는 사용될 수 없으므로
최소한의 값만 노출한다.

| 값 | 동작 |
| --- | --- |
| `default` | 서버가 결정하는 기본 경로. **현재 구현에서는 semantic과 동일** |
| `semantic` | vector 경로 강제 |
| `lexical` | pg_trgm 경로 강제 |

`default`를 별도로 둔 이유는, 기본 경로가 바뀌어도(예: 향후 RRF 활성화)
**API 계약을 바꾸지 않고** 서버에서 전환하기 위해서다.

**튜닝 파라미터는 노출하지 않는다.** `rrf_k`, `trigram_floor`, `vector_weight`,
`top_k`, `ef_search` 등은 public API에 존재하지 않는다. 값이 필요하면 서버 설정으로 관리한다.

UX 권고: `mode`를 "검색 알고리즘 선택기"로 노출하지 않는다.
결과가 없을 때 "오타/띄어쓰기까지 넓게 찾기" 같은 **의도 기반 보조 동작**으로만 쓴다.
UI 노출 여부는 OI-4다.

### 6.4 실행 순서 (규범)

```text
서버 identity
→ ACL 필터
→ current READY revision 필터 (is_deleted = FALSE)
→ chunk retrieval (전체 eligible chunk 대상)
→ document score = MAX(chunk score)
→ document ranking (동점 시 document_id ASC)
→ document 단위 pagination
```

**chunk를 top-K로 먼저 자른 뒤 문서 dedup하지 않는다**(기능명세 §11.3).
이 순서를 어기면 chunk가 많은 문서가 페이지를 잠식하고 문서 단위 `total`이 틀어진다.

### 6.5 Response

```json
{
  "items": [
    {
      "document_id": "3f2a...",
      "title": "2026년 AI 문서관리 사업계획서",
      "file_type": "hwp",
      "department": { "id": "6b1f...", "name": "기획조정실" },
      "tags": [{ "id": 12, "name": "보안" }],
      "updated_at": "2026-08-30T04:12:00Z",
      "current_revision": {
        "revision_id": "9a3c...",
        "revision_no": 4,
        "created_at": "2026-08-30T04:12:00Z"
      },
      "has_newer_revision": false,
      "snippet": "...총 사업예산은 300,000,000원이며...",
      "matched_chunk": {
        "chunk_id": "c81d...",
        "revision_id": "9a3c...",
        "section_title": null,
        "anchor": { "type": "paragraph", "paragraph_index": 42 }
      }
    }
  ],
  "page": 1,
  "size": 20,
  "total": 37
}
```

`matched_chunk`는 문서 점수를 만든 **best chunk**이며 `revision_id`는 항상
`current_revision.revision_id`와 같다(검색은 current READY revision만 사용).
같은 문서는 결과에 한 번만 나타난다.

`snippet`은 best chunk 본문에서 생성하며 원문 그대로의 짧은 발췌다.
LLM으로 생성하거나 요약하지 않는다.

### 6.6 응답에 넣지 않는 것

| 제외 항목 | 이유 |
| --- | --- |
| `score` / `confidence` / `relevance` | §7 |
| `matched_chunks_count` | §6.7 |
| `source_path`, `original_filename` | 공유폴더 경로·파일명 노출 금지 |
| chunk 전체 본문 | 응답 크기. 필요한 건 snippet뿐 |

### 6.7 `matched_chunks_count`를 v1에서 제외하는 이유

기능명세 §11.3은 이 값의 "매칭 판정"을 API Contract에서 정의하라고 위임했다. 판단은 다음과 같다.

vector 검색에는 **매칭/비매칭 경계가 없다.** 모든 chunk가 cosine 점수를 가지므로
"매칭된 chunk 수"는 사실상 "그 문서의 전체 chunk 수"가 되어 사용자에게 의미가 없고,
숫자가 클수록 관련성이 높다는 잘못된 인상을 준다.

lexical(`pg_trgm`) 경로에서는 floor 기준으로 셀 수 있지만, `mode`에 따라 의미가 달라지는
필드를 계약에 넣으면 frontend가 해석할 수 없다.

따라서 **v1 응답에서 제외한다.** UI에 문서 내 매칭 위치가 필요하다고 확인되면
"문서 내 검색" 전용 endpoint로 별도 설계한다(OI-5).

---

## 7. 검색 Score 비노출 정책

`pg_trgm` 유사도, vector cosine, RRF 점수는 **서로 척도가 다르고 비교 불가능**하다.
게다가 Search PoC에서 **정답이 없는 질의에도 cosine 0.816이 나왔다**
(정답 있는 질의 평균 0.906, 분리도 0.09).

따라서:

* `confidence`, `score`, `relevance`, `match_percent` 같은 **통합 신뢰도 필드를 만들지 않는다.**
* 사용자에게 "관련도 93%" 형태로 표시하지 않는다.
* 정렬 순서가 곧 관련도 표현이며, 그 이상을 수치로 약속하지 않는다.

내부 관측이 필요하면 **로그와 `request_id`** 로 남긴다. 응답 본문에 debug score를 넣지 않는다
(한번 넣으면 frontend가 표시하기 시작한다).

DB `chat_message_sources.rrf_score`는 내부 provenance 컬럼이며 API로 노출하지 않는다.
DB v2.4 주석대로 RRF가 OFF일 때 `NULL`이고, **vector cosine을 이 컬럼에 기록하지 않는다.**

---

## 8. Documents API

### 8.1 문서 상세

```http
GET /api/v1/documents/{document_id}
```

```json
{
  "document_id": "3f2a...",
  "title": "2026년 AI 문서관리 사업계획서",
  "file_type": "hwp",
  "department": { "id": "6b1f...", "name": "기획조정실" },
  "owner": { "id": "77c2...", "name": "홍길동" },
  "tags": [{ "id": 12, "name": "보안" }],
  "created_at": "2026-01-05T01:00:00Z",
  "updated_at": "2026-08-30T04:12:00Z",
  "current_revision": {
    "revision_id": "9a3c...",
    "revision_no": 4,
    "created_at": "2026-08-30T04:12:00Z"
  },
  "latest_revision": {
    "revision_id": "b7e0...",
    "revision_no": 5,
    "created_at": "2026-09-02T09:30:00Z"
  },
  "is_searchable": true,
  "downloadable": true
}
```

| 필드 | 의미 |
| --- | --- |
| `current_revision` | 검색·RAG에 노출되는 READY revision. 없으면 `null` |
| `latest_revision` | 공유폴더에서 마지막으로 발견된 revision (처리 중일 수 있음) |
| `is_searchable` | `current_revision != null`. 즉 `is_ready = TRUE`인 revision 보유 여부 |
| `downloadable` | 원본 파일을 지금 제공할 수 있는지 (§8.3) |

`is_searchable = false`는 처리 중이거나 본문 추출에 실패한 문서다.
`parse_result_code` 등 내부 처리 상태는 §8.2에서만 제공한다.

**오류**: 없음 또는 권한 없음 → `404 DOCUMENT_NOT_FOUND` (§3.3).

### 8.2 Revision 이력

```http
GET /api/v1/documents/{document_id}/revisions?page=1&size=20
```

`revision_no DESC` 정렬. pagination 적용.

```json
{
  "items": [
    {
      "revision_id": "b7e0...",
      "revision_no": 5,
      "content_hash": "9f86d081...",
      "file_size": 2225664,
      "source_modified_at": "2026-09-02T09:12:00Z",
      "parse_status": "RUNNING",
      "parse_result_code": null,
      "is_current": false,
      "is_ready": false,
      "created_at": "2026-09-02T09:30:00Z"
    },
    {
      "revision_id": "9a3c...",
      "revision_no": 4,
      "content_hash": "3b1a77c9...",
      "file_size": 2221001,
      "source_modified_at": "2026-08-30T03:50:00Z",
      "parse_status": "SUCCESS",
      "parse_result_code": "TEXT_EXTRACTED",
      "is_current": true,
      "is_ready": true,
      "created_at": "2026-08-30T04:12:00Z"
    }
  ],
  "page": 1, "size": 20, "total": 5
}
```

`parse_status`는 **worker 실행 상태**, `parse_result_code`는 **파싱 결과의 의미**다
(기능명세 §8.2). 두 값을 하나로 합치지 않는다.

두 필드의 값 도메인은 DB CHECK와 동일한 closed set이다.

`parse_status` — worker 실행 상태:

```text
PENDING  RUNNING  SUCCESS  FAILED
```

`parse_result_code` — 파싱 결과의 의미:

```text
TEXT_EXTRACTED  EMPTY_DOCUMENT  OCR_REQUIRED  ENCRYPTED
CORRUPT         UNSUPPORTED_FORMAT           PARSE_FAILED
```

아직 파싱 전이면 `parse_result_code`는 `null`이다.
`parse_status`에는 `SKIPPED`가 없다 — `SKIPPED`는 downstream 단계
(`embedding_status` / `summary_status` / `tagging_status`) 전용이며,
그 세 필드는 v1 응답에서 제외한다.

`parse_status = SUCCESS`이면서 `parse_result_code`가 `TEXT_EXTRACTED`가 아닌 조합은
정상이다(예: 스캔 문서를 정확히 `OCR_REQUIRED`로 판정). frontend는 이를 실패로 표시하지 않는다.

**노출하지 않는 것**: `source_path_at_ingest`, `extracted_text`, `parsed_structure`,
`parser_name`, `parser_version`, `chunking_version`, `embedding_*`, `summary_*`.
사용자가 볼 필요가 없고 내부 구성·경로를 드러낸다. 운영자용 노출은 Admin API(v1 제외)에서 다룬다.

`embedding_status` / `summary_status` / `tagging_status`도 v1 응답에서 제외한다.
사용자에게 필요한 것은 "검색 가능한가"(`is_ready`)이며 단계별 상태가 아니다.

### 8.3 원본 다운로드

```http
GET /api/v1/documents/{document_id}/download
```

* **current revision의 원본만** 반환한다.
* 응답은 binary. `Content-Type`은 파일 형식에 맞게, `Content-Disposition: attachment; filename*=UTF-8''...`
* ACL 확인 후 제공한다.
* **공유폴더의 실제 filesystem path를 응답 본문·헤더·오류 메시지 어디에도 넣지 않는다.**
  `filename`은 표시용 이름만 사용한다.

`revision_id` 지정 다운로드는 **v1에서 제공하지 않는다.**
기능명세 §5는 과거 원본 바이너리 보관 여부를 별도 저장정책으로 미뤄 두었으므로,
과거 revision 파일의 존재를 계약으로 약속할 수 없다. 저장정책 확정 후 추가한다(OI-3).

| 상황 | 응답 |
| --- | --- |
| 정상 | `200` + binary |
| 없음 / 권한 없음 | `404 DOCUMENT_NOT_FOUND` |
| `current_revision`이 없음 | `409 DOCUMENT_NOT_DOWNLOADABLE` |
| 원본이 공유폴더에서 사라짐 (`missing_since`) | `409 DOCUMENT_NOT_DOWNLOADABLE` |

### 8.4 문서 삭제

**정의하지 않는다.** 공유폴더가 Source of Truth이므로 웹 UI/API에서 원본을 삭제·수정·이동하지 않는다
(기능명세 §3.1). `DELETE /api/v1/documents/{id}`는 존재하지 않으며 `405`가 아니라 라우트 자체가 없다.

---

## 9. Chat API

### 9.1 세션 생성

```http
POST /api/v1/chat/sessions
```

```json
{ "title": "보안 사고 대응 문의" }
```

`title`은 **선택**이다(`chat_sessions.title`이 nullable). 생략하면 `null`로 만들고,
서버가 첫 질문으로 제목을 만들지 여부는 구현 재량이되 **첫 메시지 이전에 임의 제목을 만들지 않는다.**

`201 Created`

```json
{ "session_id": "5d2e...", "title": "보안 사고 대응 문의",
  "created_at": "2026-09-08T02:31:00Z", "updated_at": "2026-09-08T02:31:00Z" }
```

### 9.2 세션 목록

```http
GET /api/v1/chat/sessions?page=1&size=20
```

**본인 세션만** 반환한다(`chat_sessions.user_id` = 서버 identity). `updated_at DESC`.

```json
{
  "items": [
    { "session_id": "5d2e...", "title": "보안 사고 대응 문의",
      "message_count": 4,
      "created_at": "2026-09-08T02:31:00Z",
      "updated_at": "2026-09-08T02:40:00Z" }
  ],
  "page": 1, "size": 20, "total": 3
}
```

### 9.3 세션 상세 (메시지 포함)

```http
GET /api/v1/chat/sessions/{session_id}?page=1&size=50
```

**메시지 전용 endpoint를 따로 만들지 않는다.** 세션을 열면 항상 메시지가 필요하므로
endpoint를 나누면 왕복만 늘어난다. 메시지가 많은 세션은 이 endpoint의 pagination으로 다룬다.

`page`/`size`는 **메시지**에 적용된다(`created_at ASC`, 동점 시 `id ASC`).

```json
{
  "session_id": "5d2e...",
  "title": "보안 사고 대응 문의",
  "created_at": "2026-09-08T02:31:00Z",
  "updated_at": "2026-09-08T02:40:00Z",
  "messages": {
    "items": [
      { "message_id": "aa01...", "role": "user",
        "content": "보안 사고가 나면 어디로 보고해야 하나요?",
        "created_at": "2026-09-08T02:31:10Z" },
      { "message_id": "aa02...", "role": "assistant",
        "content": "보안 사고 발생 시 정보보안팀에 즉시 보고합니다.",
        "refused": false,
        "has_inaccessible_sources": false,
        "content_hidden": false,
        "sources": [
          { "document_id": "7c4b...", "revision_id": "1e9f...", "chunk_id": "d02a...",
            "title": "보안사고 대응 절차", "file_type": "hwpx",
            "section_title": null,
            "anchor": { "type": "paragraph", "paragraph_index": 3 },
            "accessible": true }
        ],
        "created_at": "2026-09-08T02:31:14Z" }
    ],
    "page": 1, "size": 50, "total": 4
  }
}
```

`role`은 `chat_messages.role` CHECK와 동일하게 `user` / `assistant` / `system`이다.
`system` 메시지는 v1 UI에서 표시하지 않으나 계약상 나타날 수 있다.

**과거 답변 재조회 시 ACL 재확인**(기능명세 §10.5): 저장 시점이 아니라 **조회 시점의**
ACL로 각 source의 접근 가능 여부를 다시 판정한다.

* 접근 불가한 source: `accessible: false`, 그리고 `title` / `section_title` / `anchor`를 **생략**한다
  (제목만으로도 정보가 새기 때문). `document_id` / `chunk_id`는 감사 추적을 위해 유지한다.
* 하나라도 접근 불가하면 `has_inaccessible_sources: true`.
* 정책에 따라 답변 본문을 숨겨야 하면 `content_hidden: true`, `content`는 `null`.

`content_hidden`을 별도 필드로 둔 이유는, 숨김 여부를 `content == null`로 추론하게 만들면
"본문이 비어 있는 답변"과 구분되지 않기 때문이다.

**오류**: 없음 또는 타인의 세션 → `404 CHAT_SESSION_NOT_FOUND`.
세션은 소유자만 접근하므로 v1에서 세션 조회에 403은 발생하지 않는다.

### 9.4 메시지 생성 (RAG 질의)

```http
POST /api/v1/chat/sessions/{session_id}/messages
```

```json
{ "message": "2026년 출장비 기준을 알려줘" }
```

`message`는 필수, 1~4000자. 초과 시 `422 CHAT_MESSAGE_TOO_LONG`.

`201 Created` — **텍스트만 반환하지 않는다.**

```json
{
  "message_id": "aa04...",
  "answer": "2026년 국내 출장 일비는 60,000원입니다.",
  "refused": false,
  "sources": [
    {
      "document_id": "9b12...",
      "revision_id": "44af...",
      "chunk_id": "77de...",
      "title": "2026년 출장비 지급기준",
      "file_type": "hwp",
      "section_title": null,
      "anchor": { "type": "paragraph", "paragraph_index": 2 },
      "accessible": true
    }
  ],
  "created_at": "2026-09-08T02:40:02Z"
}
```

거절 응답:

```json
{
  "message_id": "aa05...",
  "answer": "관련 문서에서 확인할 수 없습니다.",
  "refused": true,
  "sources": [],
  "created_at": "2026-09-08T02:41:10Z"
}
```

#### `refused`

**`refused`는 독립 boolean 필드이며 서버가 판정한다.**

```text
금지: answer 문자열을 파싱해 "관련 문서에서 확인할 수 없습니다."인지 비교
금지: sources 배열이 비었는지로 추론
```

문자열 비교는 문구가 바뀌면 깨지고 번역·다국어에서 무너진다.
`sources` 길이로 추론하는 것도 안 된다 — **일부 근거는 있으나 질문의 핵심에 답할 수 없어
거절하는 경우**(근거가 있으면서 `refused: true`)가 성립하기 때문이다.

`refused: true`일 때도 `sources`가 비어 있지 않을 수 있다.

`refused` 판정 기준과 threshold는 **이 계약에서 정하지 않는다.**
Design Freeze OPEN 항목이며 RAG 구현 단계에서 결정한다.
**vector cosine threshold 하나만으로 판정하지 않는다**(기능명세 §13.2).

> `refused`를 저장할 컬럼이 `chat_messages`에 없다 → **OI-2**.

#### 실행 순서 (규범)

```text
서버 identity
→ ACL 필터
→ current READY revision 필터
→ retrieval
→ LLM context 구성
→ 답변 생성
→ source validation
→ 저장 (chat_messages + chat_message_sources)
```

권한 없는 chunk는 **LLM context에 들어가지 않는다**(기능명세 §10.4).
검색된 뒤 후처리로 제거하는 구현을 허용하지 않는다.

Retrieved 문서 내용은 데이터이며 시스템 지시가 아니다(기능명세 §3.5).
문서 안의 명령형 문구를 실행하지 않는다.

**오류**

| 상황 | 응답 |
| --- | --- |
| 세션 없음 / 타인 세션 | `404 CHAT_SESSION_NOT_FOUND` |
| 타인 세션에 쓰기 시도인데 존재가 이미 노출된 경우 | `403 FORBIDDEN` |
| 메시지 길이 초과 | `422 CHAT_MESSAGE_TOO_LONG` |
| LLM 호출 실패 | `500 INTERNAL_ERROR` (부분 답변을 지어내지 않는다) |

RAG 응답은 동기 방식이다. 스트리밍은 v1 범위가 아니다(OI-6).

---

## 10. Tags API

```http
GET /api/v1/tags?page=1&size=100
```

문서 필터 UI용 태그 목록이다. `name ASC`.

```json
{ "items": [{ "id": 12, "name": "보안" }], "page": 1, "size": 100, "total": 42 }
```

**태그 CRUD는 v1에 없다.** 생성/수정/삭제/문서 태그 부여는 자동 태깅 관리 API와 함께
후속 버전에서 다룬다. AUTO 태그(`revision_tags`)와 MANUAL/SYSTEM 태그(`document_tags`)의
구분·provenance 노출도 v1 범위가 아니다(기능명세 §15.3).

---

## 11. Departments API

```http
GET /api/v1/departments
```

검색 필터/문서 상세 표시용 부서 목록이다. `name ASC`.
부서 수는 소규모이므로 **pagination 없이 전체를 반환한다.**

```json
{ "items": [{ "id": "6b1f...", "name": "기획조정실" }] }
```

응답에 `items`만 있고 `page`/`size`/`total`이 없는 유일한 목록 endpoint다.
계약을 읽는 쪽이 헷갈리지 않도록 여기에 명시한다.

---

## 12. Deferred APIs

v1에서 제외한다. 제외 이유를 함께 남긴다.

| API | 이유 |
| --- | --- |
| Favorites (`POST/DELETE /documents/{id}/favorite`, `GET /favorites`) | 기능명세 §17에서 **P2**. MVP 핵심 아님 |
| Recent views | 기능명세 §17에서 **P2** |
| 자동 요약 / 자동 태깅 조회·관리 | 기능명세 §15에서 **P2** |
| Tag CRUD, 문서 태그 부여 | 태깅 정책 확정 후 |
| Admin API (운영자용 처리 상태·재처리) | 별도 권한 모델 필요 |
| 보고서 자동 생성 | 기능명세 §19에서 **P3** |
| MCP API | 기능명세 §18에서 **P2~P3** |
| Embedding / Worker / File Sync 관리 | 운영 도구 범위 |
| 고급 ACL 관리 | DENY precedence가 OPEN |
| Revision 지정 다운로드 | 원본 바이너리 보관 정책 미확정 (OI-3) |
| 문서 내 검색 / `matched_chunks_count` | §6.7, OI-5 |
| RAG 스트리밍 응답 | OI-6 |
| `DELETE /documents/{id}` | **영구 제외** (기능명세 §3.1) |

즐겨찾기/최근 조회는 P2이므로 `DocumentSummary`에 `is_favorite` 필드를 넣지 않는다.
나중에 추가하는 것은 하위 호환이지만, 미리 넣고 항상 `false`를 채우면 계약이 거짓이 된다.

---

## 13. Open Issues

설계 문서를 수정하지 않고 기록만 한다(작업 요청 §29).

### OI-1. 검색 `year` 필터의 근거 필드가 스키마에 없다 — **결정 필요**

기능명세 §12.2와 §23.1은 "연도" 필터를 요구하지만,
**DB v2.4 어디에도 `year` 컬럼이 없다.** `documents`에도 `document_revisions`에도 없다.

가능한 근거 후보와 각각의 의미 차이:

| 후보 | 의미 | 문제 |
| --- | --- | --- |
| `document_revisions.source_mtime` | 공유폴더 파일 수정 시각 | 문서 내용상의 연도(2026년 계획서)와 다를 수 있음 |
| `documents.created_at` | 시스템이 처음 발견한 시각 | 오래된 문서를 올해로 잘못 분류 |
| 제목에서 추출 | "2026년 사업계획서" | heuristic. 실패 시 필터에서 누락 |
| 태그로 관리 | 운영자가 연도 태그 부여 | 수작업 비용 |

**결정 전까지 이 필터를 구현하지 않는다.** 계약에는 파라미터를 정의해 두되
서버는 `year` 수신 시 `400 BAD_REQUEST`(미구현)로 응답하거나 라우트에서 제외한다.
**추측한 연도로 조용히 필터링하면 사용자가 결과 누락을 알아챌 수 없다.**

### OI-2. `refused`를 저장할 컬럼이 없다 — **결정 필요**

API는 `refused` boolean을 반환해야 하지만 `chat_messages`에 해당 컬럼이 없다.

`chat_message_sources` 개수로 유도할 수 없다 — 근거가 있으면서도 거절하는 경우가 성립하기 때문이다.
저장하지 않으면 과거 세션을 다시 열 때 `refused` 값을 재현할 수 없고,
기능명세 §23.2의 **Refusal Correctness** 지표도 계산할 수 없다.

제안(결정 아님): `chat_messages.refused BOOLEAN NOT NULL DEFAULT FALSE` 또는
거절 사유까지 담는 `refusal_reason TEXT`. DB Migration 단계에서 결정한다.

### OI-3. 과거 revision 원본 다운로드 가능 여부

기능명세 §5는 과거 원본 바이너리 보관을 별도 저장정책으로 미뤄 두었다.
보관하지 않으면 revision 이력에서 과거 파일을 받을 수 없다.
UI에 revision 목록을 보여주면서 다운로드가 안 되면 혼란이 생기므로,
저장정책 확정 전까지 v1은 current revision만 제공한다.

### OI-4. `mode`의 UI 노출 여부

계약에는 정의했으나 사용자에게 검색 알고리즘 선택기로 노출할지는 UX 결정이다.
권고는 "노출하지 않고, 결과 없음일 때 보조 동작으로만 사용"이다.

### OI-5. 문서 내 매칭 위치 표시 필요 여부

`matched_chunks_count`를 제외한 대신(§6.7), UI가 "문서 안 어디에 몇 건" 정보를 필요로 하는지
확인이 필요하다. 필요하면 문서 내 검색 endpoint로 별도 설계한다.

### OI-6. RAG 응답 스트리밍

v1은 동기 응답이다. 답변 지연이 UX 문제가 되면 SSE 등을 별도 계약으로 추가한다.
`refused`·`sources`가 스트림 종료 시점에 확정되므로 계약 형태가 달라진다.

### OI-7. `q` 최대 길이와 rate limit 수치

`q` 512자, `message` 4000자는 계약 초안값이다. 실제 한도와 rate limit 정책은
운영 요구에 따라 조정한다. 조정 시 이 문서와 OpenAPI를 함께 갱신한다.

---

## 14. FastAPI / OpenAPI 관계

```text
구현 전  : 이 문서(api-contract-v1.md)가 계약
구현 후  : FastAPI가 생성하는 OpenAPI schema가 runtime source of truth
```

두 문서가 갈라지면 계약의 의미가 사라지므로, 구현 시 다음을 **테스트로 강제**할 수 있게 설계한다.

1. **경로 집합 일치** — OpenAPI의 path+method 집합이 §15 표와 정확히 일치.
   특히 `DELETE /documents/{id}`가 **없어야 한다**는 것을 테스트한다.
2. **오류 스키마** — 모든 4xx/5xx 응답이 `error.code`/`message`/`request_id`를 갖는다.
3. **오류 코드 closed set** — 구현이 반환하는 코드가 §3.4 표를 벗어나지 않는다.
4. **금지 필드 부재** — 응답 스키마 어디에도 `score`, `confidence`, `relevance`,
   `source_path`, `original_filename`, `extracted_text`, `parsed_structure`가 없다.
5. **enum 일치** — `file_type`, `parse_status`, `parse_result_code`, `role`이
   DB v2.4의 CHECK 값 집합과 동일하다.
6. **pagination 봉투** — 목록 응답이 `items`/`page`/`size`/`total`을 갖는다
   (§11 departments는 명시적 예외).

이 검사는 OpenAPI JSON을 읽는 계약 테스트로 구현하며, 실제 DB나 LLM을 필요로 하지 않는다.

---

## 15. Endpoint 요약

| Method | Path | 인증 | ACL | 설명 |
| --- | --- | --- | --- | --- |
| `GET` | `/api/v1/search` | 필수 | 선필터 | 검색 / 필터 브라우징 |
| `GET` | `/api/v1/documents/{document_id}` | 필수 | 문서 | 문서 상세 |
| `GET` | `/api/v1/documents/{document_id}/revisions` | 필수 | 문서 | revision 이력 |
| `GET` | `/api/v1/documents/{document_id}/download` | 필수 | 문서 | 원본 다운로드 (binary) |
| `POST` | `/api/v1/chat/sessions` | 필수 | 본인 | 세션 생성 |
| `GET` | `/api/v1/chat/sessions` | 필수 | 본인 | 세션 목록 |
| `GET` | `/api/v1/chat/sessions/{session_id}` | 필수 | 본인 | 세션 + 메시지 |
| `POST` | `/api/v1/chat/sessions/{session_id}/messages` | 필수 | 본인 + 문서 | RAG 질의 |
| `GET` | `/api/v1/tags` | 필수 | — | 태그 목록 |
| `GET` | `/api/v1/departments` | 필수 | — | 부서 목록 |

**10개 endpoint.** 이 표에 없는 경로는 v1에 존재하지 않는다.

---

## 16. v2.4 설계 문서와의 매핑

| API 요소 | 근거 |
| --- | --- |
| ACL 선필터 순서 | 기능명세 §10.3 / §10.4 |
| current READY revision만 검색 | 기능명세 §11.2, DB `is_ready` 생성식 |
| document aggregation 순서 | 기능명세 §11.3 |
| `mode` 3값 / 튜닝 파라미터 비노출 | Design Freeze (Vector 기본, pg_trgm 별도, RRF OFF) |
| score 비노출 | Search PoC no-answer 분리도 0.09, 기능명세 §24 |
| `Anchor` union | 기능명세 §14.1 (format-aware citation) |
| `paragraph_index` ↔ `paragraph_start/end` | 기능명세 §14.1 DB 매핑 |
| `parse_status` / `parse_result_code` 분리 | 기능명세 §8.2, DB §7 |
| `file_type` 소문자 | DB `documents.file_type` CHECK |
| `role` 값 | DB `chat_messages.role` CHECK |
| 과거 답변 ACL 재확인 | 기능명세 §10.5 |
| `refused` 문자열 파싱 금지 | 기능명세 §13.2 |
| DELETE 미제공 | 기능명세 §3.1 |
| Favorites/Recent 제외 | 기능명세 §17 (P2) |

이 계약은 위 문서들의 **하위 계층**이다. 충돌이 발견되면 설계 문서가 우선이며,
계약을 임의로 바꾸지 않고 Open Issue로 올린다.
