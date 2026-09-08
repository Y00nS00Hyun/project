# HTTP API — Search / Documents (v1)

구현: `src/api/` · 테스트: `tests/test_api_contract.py`, `tests/test_api_http.py`

[API Contract v1](api-contract-v1.md)의 **Search / Document 범위**를 FastAPI로 구현한 것이다.
Chat/RAG endpoint는 계약에 정의돼 있으나 **이번 범위가 아니며 stub도 만들지 않았다.**
계약·설계 문서는 수정하지 않았다.

---

## 1. Application

```text
entrypoint:  src/api/app.py  →  app = create_app()
prefix:      /api/v1
실행:        uvicorn api.app:app   (PYTHONPATH=src)
```

필요한 환경변수:

```text
DATABASE_URL          postgresql://user:pass@host:5432/dbname
SHARED_ROOT           /mnt/shared          다운로드 경로 검증 기준
APP_ENV               production | development | test
CORS_ALLOW_ORIGINS    쉼표 구분 allow-list (미설정이면 CORS 미적용)
```

`APP_ENV`가 production이 아닐 때만 `/docs`와 `/openapi.json`이 열린다.
CORS는 기본값이 없다 — 자격증명이 오가는 API에서 `*`는 사용자가 방문하는 모든 사이트를
이 API의 클라이언트로 만든다.

### 의존성 수명

| 대상 | 수명 | 이유 |
| --- | --- | --- |
| 설정 / DSN | 프로세스 1회 (`lru_cache`) | 요청마다 다시 읽을 이유가 없다 |
| DB connection | **요청 1개당 1개**, 종료 시 close | |
| 임베딩 모델 | 프로세스 1개, **첫 semantic 질의에 lazy load** | 요청마다 로드하면 모든 검색이 모델 로드 비용을 낸다. startup에 eager load하면 모델 캐시가 없는 배포에서 lexical·browse까지 뜨지 못한다 |

---

## 2. Authentication

**SSO는 아직 구현되지 않았다.** identity를 서버가 결정하는 추상화만 두었다.

```python
require_user(request) -> AuthenticatedUser(user_id, department_id)
```

핵심 규칙: **클라이언트가 보낸 값을 identity로 쓰지 않는다.** `user_id`는 검색 API의
허용 parameter 목록에 아예 없어서, 넣으면 422로 거절된다.

### 개발용 provider

`APP_ENV`가 production이 **아닐 때만** `X-Debug-User-Id` 헤더를 받는다.
그 값도 실제 존재하고 활성화된 사용자여야 한다.

production에서는 헤더를 읽지 않고 `401 UNAUTHENTICATED`로 거절한다 —
인증 공급자가 없는 배포는 **닫힌 상태로 실패**해야지 클라이언트 값을 신뢰해서는 안 된다.

---

## 3. Routes

| Method | Path | 인증 | ACL |
| --- | --- | --- | --- |
| GET | `/api/v1/search` | 필수 | 선필터 |
| GET | `/api/v1/documents/{document_id}` | 필수 | 문서 |
| GET | `/api/v1/documents/{document_id}/revisions` | 필수 | 문서 |
| GET | `/api/v1/documents/{document_id}/download` | 필수 | 문서 |
| GET | `/api/v1/tags` | 필수 | — |
| GET | `/api/v1/departments` | 필수 | — |

**6개뿐이다.** 쓰기 메서드(POST/PUT/PATCH/DELETE)는 하나도 없다 —
공유폴더가 source of truth이고 시스템은 원본을 수정하지 않는다.

---

## 4. Service reuse

router는 **검증과 변환만** 한다. SQL을 직접 쓰지 않고 권한·랭킹·집계를 재구현하지 않는다.

```text
Router → SearchService / DocumentService → Repository → PostgreSQL
```

* 검색은 기존 `search.SearchService`를 그대로 호출한다. ACL-before-retrieval,
  document MAX 집계, 결정적 정렬은 전부 기존 백엔드가 담당한다.
* `default → semantic` 해석도 service가 한다. router는 mode 문자열만 파싱한다.
* 문서 상세/이력/다운로드 인가는 `api.document_service.DocumentService`가 담당한다.

계약 응답에 필요한 `tags` / `has_newer_revision` / `current_revision`은 router에서 별도
질의하지 않고 **search repository를 확장해** 한 질의에서 가져온다. `file_type` 필터도
같은 이유로 백엔드에 추가했다.

---

## 5. Search

```http
GET /api/v1/search?q=...&mode=...&page=1&size=20
                  &department_id=...&year=2026&tag_id=1&tag_id=2&file_type=hwpx
```

| mode | 동작 |
| --- | --- |
| `default` (기본) | service가 결정 — 현재 semantic |
| `semantic` | vector exact cosine |
| `lexical` | pg_trgm |
| `q` 없음 | browse. **retrieval도 임베딩도 하지 않는다** |

browse 응답은 `snippet = null`, `matched_chunk = null`이다.

**정의되지 않은 query parameter는 무시하지 않고 422다.** `yer=2026` 같은 오타가 조용히
무시되면 사용자는 필터가 걸린 줄 알고 걸리지 않은 결과를 본다.

`q`는 512자를 넘으면 `SEARCH_QUERY_TOO_LONG`(422)이다.

### 응답에 없는 것

`retrieval_score`를 응답에 넣지 않는다. `score` / `confidence` / `similarity` /
`rrf_score` / `distance` 같은 필드도 만들지 않는다. cosine과 trigram 유사도는 서로
비교 불가능한 척도이고, Search PoC에서 정답 없는 질의에도 cosine 0.816이 나왔다 —
숫자로 노출하면 UI가 그것을 확신도로 표시하게 된다.

`source_path` / `original_filename` / `extracted_text` / `parsed_structure`도 없다.

---

## 6. Documents

### 상세

`current_revision`(검색 노출본)과 `latest_revision`(마지막 발견본)을 구분해서 준다.
`is_searchable`은 `current_revision != null`이다.
`content_hash`, parser 내부 상태, 경로는 노출하지 않는다.

### Revision 이력

`revision_no DESC`. `parse_status`(worker 실행 상태)와 `parse_result_code`(결과 의미)를
합치지 않는다 — `SUCCESS` + `OCR_REQUIRED`는 정상 조합이다.

`is_current`는 **latest가 아니라 current** 기준이다. 더 최신 revision이 FAILED면
그것은 latest일 뿐 current가 아니다.

`error_message`는 반환하지 않는다.

### 다운로드

current revision의 원본만, read-only로 제공한다. 복사본을 만들거나 원본을 수정하지 않는다.

---

## 7. Download security

순서가 핵심이다.

```text
1. 문서 조회 + ACL          ← 여기서 막히면 파일 접근 자체가 일어나지 않는다
2. current revision 확인
3. source_path → shared root 경계 재검증
4. 파일 open
```

`source_path`는 **데이터**다. 저장돼 있다는 이유로 신뢰하지 않고, 열기 전에
`resolve_source_path()`로 shared root 내부인지 다시 확인한다(ingestion과 같은 함수).
`../` traversal과 symlink escape 모두 거부되며, 두 경우 모두 파일 내용이 응답에
들어가지 않는 것을 테스트로 고정했다.

응답 본문·헤더·오류 메시지 어디에도 filesystem 경로를 넣지 않는다.
`Content-Disposition`의 `filename`은 표시용 이름뿐이다.

| 상황 | 응답 |
| --- | --- |
| 정상 | 200 + binary |
| 없음 / 권한 없음 | 404 `DOCUMENT_NOT_FOUND` |
| current revision 없음 | 409 `DOCUMENT_NOT_DOWNLOADABLE` |
| 원본 파일 사라짐 / 경계 밖 | 409 `DOCUMENT_NOT_DOWNLOADABLE` |

---

## 8. Error mapping

모든 오류는 계약의 봉투를 쓴다. FastAPI 기본 `{"detail": ...}`는 외부로 나가지 않는다.

```json
{"error": {"code": "DOCUMENT_NOT_FOUND", "message": "문서를 찾을 수 없습니다.",
           "request_id": "..."}}
```

| 상황 | status | code |
| --- | --- | --- |
| 인증 없음 / 알 수 없는 사용자 | 401 | `UNAUTHENTICATED` |
| 파라미터 오류·범위 초과·알 수 없는 parameter·잘못된 mode | 422 | `VALIDATION_ERROR` |
| 검색어 512자 초과 | 422 | `SEARCH_QUERY_TOO_LONG` |
| 문서 없음 **또는 권한 없음** | 404 | `DOCUMENT_NOT_FOUND` |
| 원본 제공 불가 | 409 | `DOCUMENT_NOT_DOWNLOADABLE` |
| semantic 모델 사용 불가 | 500 | `INTERNAL_ERROR` |
| 그 외 예외 | 500 | `INTERNAL_ERROR` |

**권한 없는 문서에 403을 주지 않는다.** 403은 그 문서가 존재하고 id가 유효하다는 것을
확인해 주며, 이는 ACL 선필터가 감추려는 정보 그 자체다.

Pydantic 검증 오류는 필드명과 짧은 사유만 `details`로 옮긴다. 원본 payload를 그대로
넘기면 제출값과 내부 모델 구조가 함께 새어 나간다.

예상 못 한 예외는 로그에만 상세를 남기고 클라이언트에는 `request_id`만 준다.
`X-Request-Id`는 응답 헤더와 오류 본문이 같은 값을 쓰고, upstream 헤더가 있으면 보존한다.

로그에는 request_id / method / route / status / duration만 남긴다.
검색어 전체·문서 본문·파일 경로는 남기지 않는다.

---

## 9. Degradation

임베딩 모델을 못 불러도 semantic 경로만 죽는다.

```text
semantic  → 500 INTERNAL_ERROR
lexical   → 정상
browse    → 정상
detail / revisions / download / tags / departments → 정상
```

모델은 첫 semantic 질의에만 lazy load되므로 그 외 경로는 모델을 건드리지 않는다.

---

## 10. Known limitations

1. **SSO 미구현.** 개발용 헤더 provider만 있고 production은 401로 닫혀 있다.
2. **Chat/RAG 없음.** 계약에 있으나 구현하지 않았고 stub도 만들지 않았다.
3. **rate limiting 없음.** `RATE_LIMITED` 코드는 정의만 돼 있다.
4. **HTTP Range/streaming 미지원.** 일반 `FileResponse`이며 대용량 튜닝은 하지 않았다.
5. **semantic 검색에 relevance floor가 없다** — §11 AN-1.
6. **health/readiness endpoint 없음.** 계약에 없는 public endpoint를 임의로 만들지 않았다.
7. **tags/departments는 ACL 범위가 아니다** — §11 AN-2.

---

## 11. Architecture Notes

계약·설계 문서를 수정하지 않고 기록만 한다.

### AN-1. semantic 검색에 relevance floor가 없다 — UX 영향

vector 검색은 임계값 없이 **eligible 문서 전체**를 점수순으로 반환한다. 질의와 전혀
무관해도 유사도 0으로 결과에 남는다. 즉 semantic 모드에서 "검색 결과 없음"은 사실상
발생하지 않는다(권한 범위가 비어 있을 때만).

lexical 모드는 trigram floor가 있어 0건이 정상적으로 나온다.

계약에도 Design Freeze에도 검색 임계값 정의가 없고, RAG refusal threshold는 명시적으로
OPEN이다. 그래서 **임의로 cutoff를 만들지 않았다.** UI가 "관련 문서 없음"을 표시해야
한다면 임계값 정책 결정이 선행되어야 한다.

### AN-2. tags / departments는 ACL로 좁히지 않는다

계약(§10, §11)은 두 endpoint를 "검색 필터 UI용 목록"으로만 정의하고 노출 범위를 말하지
않는다. 현재는 전체 목록을 반환한다.

부서명·태그명 자체가 조직 구조 정보이므로 "접근 가능한 문서에 실제로 붙은 태그/부서만"
으로 좁히는 편이 안전할 수 있다. **임의로 확대하거나 축소하지 않았다** — 결정이 필요하다.

### AN-3. 계약 예시와 요청서 예시의 필드 차이

작업 요청서 예시에는 search 응답의 `year`와 revision 응답의 `document_year`가 있으나,
**API Contract v1에는 두 필드가 정의돼 있지 않다.** 계약을 authoritative source로 삼아
구현했으므로 응답에 없다.

`year`는 검색 **필터**로는 동작한다(§6.1). 즉 UI는 연도로 거를 수는 있지만 결과 카드에
연도를 표시할 수는 없다. 필요하면 계약에 필드를 추가하는 결정이 필요하다.

### AN-4. 계약 §6.1의 `year` 관련 서술이 낡았다

계약은 `year` 필터를 "OI-1 참조, 근거 필드 미확정"으로 두고 "결정 전까지 구현하지 않고
400을 반환"하라고 적고 있다. 그러나 **OI-1은 DB v2.5에서
`document_revisions.document_year` 추가로 해소**되었고 ingestion이 값을 채우고 있다.

따라서 400이 아니라 실제 필터로 구현했다. 계약의 해당 문장은 갱신이 필요하다.

### AN-5. `file_type` 필터는 계약에 있으나 백엔드에 없었다

계약 §6.1이 정의한 `file_type` 필터가 search 백엔드에 없어 이번에 추가했다
(eligible CTE의 다른 필터와 동일하게 ACL과 AND 결합). 설계 변경이 아니라 누락 보완이다.
