# ACL-aware Search Backend

구현: `src/search/` · 테스트: `tests/test_search_models.py`, `tests/test_search_backend.py`

기준 문서는 [기능명세 v2.4](functional-spec-v2.4.md), [DB 스키마 v2.5](database-schema-v2.5.md),
[Design Freeze v1](design-freeze-v1.md), [API Contract v1](api-contract-v1.md)다.
이번 구현으로 그 문서들과 migration을 수정하지 않았다.

```text
authenticated user → ACL filter → current READY revisions
    → vector / pg_trgm retrieval → document aggregation → ranked page
```

FastAPI endpoint / RAG / UI는 이 단계에 없다.

---

## 1. ACL ordering

**ACL은 retrieval 이전에 적용한다.** 모든 질의는 `eligible` CTE에서 시작하고,
retrieval은 그 CTE가 만든 행에만 실행된다. 권한 없는 문서는 점수 계산도, 랭킹도,
`total` 집계도 되지 않는다 — 후보 집합에 아예 들어오지 않는다.

```sql
eligible AS (
    SELECT ... FROM documents d
    JOIN document_revisions r ON r.id = d.current_revision_id AND r.document_id = d.id
    LEFT JOIN departments dep ON dep.id = d.department_id
    WHERE d.is_deleted = FALSE
      AND d.current_revision_id IS NOT NULL
      AND r.is_ready = TRUE
      AND EXISTS ( document_permissions 로 user_id 또는 부서 매칭 )
      AND (필터: department / year / tag)
)
```

`current_ready_chunks` VIEW는 "삭제 아님 + current revision + READY"를 이미 캡슐화하지만
**ACL이 없다.** 따라서 단독으로 쓰지 않고 항상 `eligible`과 조인한다. VIEW를 쓰는 이유는
READY 판정 규칙이 스키마와 검색 코드 사이에서 갈라지지 않게 하기 위함이다.

### 실측 확인

문서 400건 중 3건만 권한이 있는 상태에서 semantic 질의 실행 계획을 확인했다.

```text
eligible documents: 3 / 400
chunks 스캔 행 수:  권한 있는 문서의 chunk만  (400이면 ACL이 늦게 걸린 것)
Execution Time:     0.639 ms
```

### 권한 모델

| 항목 | 값 |
| --- | --- |
| user permission | `document_permissions.user_id = 인증 사용자` |
| department permission | `document_permissions.department_id = 사용자의 부서` |
| 읽기 허용 등급 | `READ` / `WRITE` / `ADMIN` |
| 기본값 | **default deny** — 권한 row가 없으면 보이지 않는다 |
| precedence | allow-only. 둘 다 있으면 높은 쪽(`ADMIN > WRITE > READ`) |
| DENY | 미구현. Open Issue 유지 |

사용자 identity는 호출자가 인증 세션에서 넘긴다. `SearchRequest`에 클라이언트가
다른 사용자로 검색할 수 있는 필드는 존재하지 않는다.

---

## 2. Search modes

| mode | 동작 |
| --- | --- |
| `default` | 서버가 결정. **현재 semantic** |
| `semantic` | vector (exact cosine) |
| `lexical` | pg_trgm |
| (q 없음) | browse — retrieval 없음 |

`default`를 enum alias가 아니라 service에서 해석하는 이유는, 기본 경로가 바뀌어도
(예: 향후 RRF) request 계약과 호출자를 건드리지 않기 위해서다.

알 수 없는 mode는 `InvalidSearchModeError`다. FastAPI 422 매핑은 다음 단계다.

RRF는 기본 적용하지 않는다. 이번 구현에 RRF 코드를 넣지 않았다.

---

## 3. Semantic path

query는 문서와 **같은 모델·같은 pinned revision·같은 벡터 공간**을 써야 한다.
그래서 새 loader를 만들지 않고 ingestion의 `LocalE5Model`을 그대로 재사용하고,
거기에 `embed_query()`만 추가했다.

```text
query: {text}      ← query 접두사. passage: 와 혼동하면 검색 품질이 조용히 나빠진다
L2 normalize
validate_vector()  ← 문서 벡터와 동일한 검증 (384d / finite / unit norm)
```

검증을 거치는 이유는, 잘못된 query 벡터는 오류 없이 **의미 없는 거리**를 만들기 때문이다.

SQL:

```sql
scored AS (
    SELECT DISTINCT ON (c.document_id)
           c.document_id, c.chunk_id, ...,
           1 - (c.embedding <=> :query_vector) AS score
    FROM current_ready_chunks c
    JOIN eligible e ON e.document_id = c.document_id
    WHERE c.embedding IS NOT NULL
    ORDER BY c.document_id, c.embedding <=> :query_vector, c.chunk_index
)
```

exact cosine(`<=>`)을 쓰고 HNSW는 만들지 않는다(Design Freeze).

모델 inference는 DB 연결 밖에서 수행한다.

---

## 4. Lexical path

migration이 만든 GIN trigram index를 사용한다.

| 대상 | 인덱스 |
| --- | --- |
| `documents.title` | `idx_documents_title_trgm` (`gin_trgm_ops`) |
| `document_revisions.extracted_text` | `idx_revisions_extracted_text_trgm` (`gin_trgm_ops`) |

WHERE 절은 `<%`(word_similarity 임계) 연산자를 쓴다. `word_similarity(...) >= x` 형태는
어떤 인덱스도 가속할 수 없다.

threshold는 설정값 하나를 모든 질의에 적용한다.

```text
TRIGRAM_THRESHOLD=0.20   (기본값, PoC의 provisional floor)
```

`<%`는 이 값을 GUC에서 읽으므로 트랜잭션 로컬로 설정한다.

```sql
SELECT set_config('pg_trgm.word_similarity_threshold', :threshold, true)
```

`SET`은 파라미터를 받지 않으므로 `set_config()`를 쓴다 — 문자열 조립이 아니다.
threshold는 API로 노출하지 않고 질의마다 바꾸지 않는다.

문서 점수는 `GREATEST(title 유사도, extracted_text 유사도)`다. 복잡한 가중치 연구는 하지 않았다.

matched chunk는 별도로 찾는다. 제목만 일치한 문서도 결과에는 나오되 인용할 chunk가
없을 수 있으므로 LEFT JOIN이다.

---

## 5. Browse path

`q`가 없거나 공백뿐이면 **retrieval을 실행하지 않는다.** 임베딩 모델도 건드리지 않는다.

```text
ACL + current READY + 필터 → ORDER BY updated_at DESC, document_id ASC
```

`snippet`과 `matched_chunk`는 `None`, `retrieval_score`도 `None`이다.

---

## 6. Document aggregation

```text
eligible chunk 전체 평가
  ↓
document별 MAX score      (DISTINCT ON (document_id) ORDER BY ..., score DESC)
  ↓
document ranking           (score DESC, document_id ASC)
  ↓
document 단위 LIMIT/OFFSET
```

**chunk top-K를 먼저 자르고 dedup하지 않는다.** 그렇게 하면 강한 chunk가 많은 문서 하나가
페이지를 독식하고 `total`도 틀어진다. 테스트로 고정했다 — chunk 30개짜리 문서와 1개짜리
문서를 `size=2`로 검색하면 둘 다 나온다.

`total`은 `count(*) OVER ()`로 LIMIT 이전 문서 수를 한 질의에서 얻는다.

---

## 7. Filters

| filter | 적용 대상 |
| --- | --- |
| `department_id` | `documents.department_id` |
| `year` | **current READY revision**의 `document_year` |
| `tag_ids` | `document_tags`. 여러 개면 AND(전부 보유) |

전부 `eligible` CTE 안에서 ACL과 **AND**로 결합된다. 필터가 ACL을 우회할 수 없다 —
권한 없는 부서로 필터링하면 결과는 0건이다.

year는 `source_mtime`을 쓰지 않는다. 아직 승격되지 않은 revision의 연도는 검색에
반영되지 않으며, 승격 후에만 반영된다.

---

## 8. Pagination / ranking

```text
score DESC, document_id ASC
```

동점 tie-break를 `document_id ASC`로 고정해 페이지 경계에서 중복·누락이 없다.
문서 10건을 4/4/2로 나눠 읽으면 정확히 10건이 한 번씩 나온다.

---

## 9. Internal score

`DocumentResult.retrieval_score`는 **랭킹 전용**이다.

`confidence`라고 부르지 않는다. cosine과 trigram 유사도는 서로 비교 불가능한 척도이고,
Search PoC에서 정답이 없는 질의에도 cosine 0.816이 나왔다(정답 있는 질의 평균 0.906).
`confidence`라는 이름은 UI가 그것을 "82% 확신"으로 표시하도록 유도한다.

API 응답으로 내보내지 않는다. 이 규칙은 dataclass 필드 이름 테스트로 고정했다.

---

## 10. Security

| 항목 | 처리 |
| --- | --- |
| ACL 순서 | retrieval 이전. 실행 계획으로 확인 |
| default deny | 권한 row 없으면 비노출 |
| SQL injection | 전부 파라미터 바인딩. 적대적 질의 4종 테스트 |
| identity 위조 | request에 user를 바꿀 수 있는 필드 없음 |
| 외부 호출 | 없음. TCP 차단 상태에서 semantic 검색 동작 확인 |
| 질의어 로깅 | full text 미기록. SHA-256 앞 12자만 |

### 권한 누출 테스트

권한 없는 문서를 **질의와 가장 잘 맞는 문서로 의도적으로 만들어** 검증한다.

```text
User A query: "인사 평가"
권한 없는 문서: "극비 인사 평가 자료"  (완전 일치)
→ 결과 0건, total 0
```

semantic / lexical / browse 세 경로 모두에서 검증했다.

---

## 11. Known limitations

1. **RRF 미구현.** optional hook도 만들지 않았다. 활성화는 Design Freeze 변경 정책을 따른다.
2. **HNSW 없음.** exact cosine이라 chunk 수가 늘면 재검토가 필요하다.
3. **lexical 점수 정책이 단순하다.** `GREATEST(title, body)`이며 가중치 튜닝을 하지 않았다.
4. **snippet은 단순 앞부분 절단이다.** 하이라이트 엔진이 아니다.
5. **DENY ACL 미지원.** allow-only이며 precedence는 Open Issue다.
6. **q 있는 semantic 검색은 항상 전체 eligible chunk를 평가한다.** 정확성을 위한 선택이며
   대규모에서 비용 측정이 필요하다.
7. **tag는 `document_tags`만 본다.** AUTO 태그(`revision_tags`)는 필터 대상이 아니다.

---

## 12. Architecture Notes

### AN-1. trigram index는 정상이지만 작은 테이블에서는 선택되지 않는다

`<%` 질의에 대해 `idx_documents_title_trgm` / `idx_revisions_extracted_text_trgm`이
**사용 가능함을 확인했다**(`enable_seqscan=off`로 강제 시 둘 다 사용됨).
400행 규모에서는 플래너가 seq scan을 선택하는데, 이는 소규모 테이블에서 정상적인 판단이다.

`gin_trgm_ops`가 지원하는 연산자에 `%>`가 포함되고 `a <% b`는 `b %> a`의 commutator이므로
질의 형태와 인덱스가 일치한다. **문제 없음**이며 실제 코퍼스 규모에서 재확인하면 된다.

### AN-2. `extracted_text`의 `[문단]` / `[표]` 마커는 lexical 매칭을 깨지 않았다

Ingestion Foundation의 AN-4 확인 항목이다. 마커가 포함된 `extracted_text`에 대해
`사업계획`, `기획실`, `300000000` 세 질의를 실행해 모두 정상 매칭됨을 확인했다
(`test_lexical_is_unaffected_by_extracted_text_markers`).

trigram은 문자 n-gram이라 마커가 별도 토큰 경계를 만들지 않는다.
**normalizer 재설계가 필요하지 않다.**

### AN-3. `document_year` 필터의 접근 경로

전용 인덱스는 없다(DB v2.5 §26의 판단대로). `eligible` CTE가 ACL로 이미 좁힌
문서 집합에서 `current_revision_id` PK 조인 후 필터하므로, 연도 인덱스가 구동
인덱스가 될 이유가 없다. 현재 구조에서는 추가 인덱스가 불필요하다.

### AN-4. exact vector 질의 비용

400문서/400chunk 규모에서 semantic 질의 0.6 ms다. ACL이 후보를 먼저 좁히므로
비용은 전체 코퍼스가 아니라 **사용자가 접근 가능한 chunk 수**에 비례한다.
권한 범위가 넓은 사용자가 많은 환경에서는 HNSW 도입 판단이 필요하지만,
이번 단계에서는 판단하지 않는다.
