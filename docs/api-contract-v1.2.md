# 사내 문서 관리 시스템 — API Contract v1.2

**v1.1에 대한 additive 개정이다.** `docs/api-contract-v1.md`와 `docs/api-contract-v1.1.md`는
그대로 두고 이 문서가 그 위의 차이만 기술한다. v1 / v1.1 클라이언트는 수정 없이 계속 동작한다.

```text
기준 문서   docs/api-contract-v1.md, docs/api-contract-v1.1.md   (변경하지 않음)
개정 성격   additive only
method/path 조합   11 → 11   (신규 엔드포인트 없음)
```

공통 envelope와 페이지네이션 규약은 전부 그대로다. 오류 코드는 하나만 추가된다.
이 문서는 다음만 추가한다.

```text
추가   POST /api/v1/chat/sessions 의 optional document_id
추가   SessionOut / SessionListItem / SessionDetail 의 document_scope
추가   GET /api/v1/documents/{document_id} 의 summary
추가   GET /api/v1/documents/{document_id} 의 chat (capability)
추가   error code FEATURE_UNAVAILABLE (503)
```

**엔드포인트를 하나도 추가하지 않는다.** document 전용 대화는 새 리소스가 아니라 기존
session의 속성이고, 요약은 새 리소스가 아니라 document detail의 필드다. 각각에 별도
엔드포인트를 두면 "scope를 적용하는 경로"와 "적용하지 않는 경로"가 둘 다 존재하게 되는데,
그 둘 중 하나를 잊는 것이 바로 이 기능에서 일어날 수 있는 가장 큰 사고다.

DB 변경은 `chat_sessions.document_id` 한 컬럼뿐이다. 요약은 `document_revisions`에 이미
있는 `summary`, `summary_status`, `summary_provider`, `summary_model`,
`summary_prompt_version`, `summarized_at`를 그대로 쓰고, SUMMARIZE는 이미
`processing_jobs.job_type` CHECK에 있는 값이다.

---

## 20. Document-scoped chat session (신규)

### 20.1 성격 — session의 속성이지 message의 파라미터가 아니다

scope는 session을 만들 때 한 번 정해지고 서버에 저장된다. 이후 모든 질문은 기존
`POST /chat/sessions/{session_id}/messages`를 그대로 쓰며, **요청 본문에 document를 싣지
않는다.**

message마다 document_id를 받는 설계를 택하지 않은 이유는 하나다. 그 설계에서는 모든 요청이
검사 대상이 되고, 검사 한 번을 빠뜨리면 그 요청만 조용히 전체 corpus를 대상으로 동작한다.
저장된 scope는 잊어버릴 수가 없다.

### 20.2 강제 지점 — prompt가 아니라 candidate SQL

scope는 `src/search/repository.py`의 `ELIGIBLE_CTE`에서 ACL 바로 옆, 같은 단계에 적용된다.

```sql
AND {READ_ACL_PREDICATE}
AND (%(scope_document_id)s::uuid IS NULL OR d.id = %(scope_document_id)s::uuid)
```

즉 scope를 벗어난 문서는 **점수가 매겨지지 않고, 순위에 들어오지 않고, 후보 집합에
들어오지도 않는다.** prompt에 "이 문서만 보라"고 적는 방식은 쓰지 않는다. 그것은 모델의
협조에 의존하는 통제이고, retrieved text는 신뢰할 수 없는 데이터라는 이 시스템의 전제와
모순된다.

### 20.3 Request

```text
POST /api/v1/chat/sessions
{
  "title": "서버 장애 대응 지침",   // optional, 기존과 동일
  "document_id": "uuid"            // optional, 신규
}
```

`document_id`가 없으면 기존과 완전히 동일한 전체 corpus session이다.

`document_id`가 있으면 **세션 생성 시점에 그 문서에 대한 read 권한을 검사한다.** 검사는
INSERT와 같은 문장 안에서 이루어지므로, 검사와 쓰기 사이에 권한이 회수되는 창이 없다.
권한이 없거나, 문서가 없거나, soft delete 되었거나, uuid 형식이 아니면 전부 동일하게
`404 DOCUMENT_NOT_FOUND`다. 구분해서 알려주면 오류 자체가 문서의 존재를 확인해 준다.

### 20.4 Response — `document_scope`

`SessionOut`, `SessionListItem`, `SessionDetail` 모두에 추가된다.

```text
document_scope: null                                     // 전체 corpus session
document_scope: { document_id, accessible: true, title }  // 읽을 수 있는 문서
document_scope: { document_id, accessible: false }        // 권한을 잃은 문서
```

`accessible`로 판별되는 discriminated union이다. `title`은 편의 정보이므로 과거 citation과
똑같은 규칙을 따른다 — **읽을 때마다 현재 권한을 다시 확인하고, 권한을 잃었으면 제목을
내려주지 않는다.** 세션을 만든 사람이므로 "이 대화는 어떤 문서에 대한 것이었다"는 사실
자체는 남지만, 그 문서가 무엇이었는지는 남지 않는다.

### 20.5 권한이 회수된 뒤

- 과거 대화 열람: 가능하다. 단 `document_scope.accessible = false`이고 제목이 없다.
  개별 message의 citation은 기존 규칙대로 `accessible: false`가 되고 본문이 가려진다.
- 새 질문: `404 DOCUMENT_NOT_FOUND`. 후보가 0건이 되어 "확인할 수 없습니다"로 끝나게
  두지 않고 명시적으로 막는다.

### 20.6 새 revision

scope는 document 단위이고 revision 단위가 아니다. 문서가 재수집되어 current revision이
바뀌면 **새 질문은 자동으로 새 revision을 근거로 삼는다.** 과거 답변의 citation은
`chat_message_sources.chunk_id`가 `ON DELETE RESTRICT`로 고정되어 있어 그대로 옛 revision을
가리킨다.

---

## 21. Document summary (신규)

### 21.1 성격 — 미리 계산된 값이지 요청 시 생성이 아니다

`GET /api/v1/documents/{document_id}`는 **어떤 경우에도 LLM을 호출하지 않는다.** 문서를
여는 동작이 외부 서비스의 가용성·지연·비용에 묶여서는 안 되고, 누군가 클릭했다는 이유로
문서가 밖으로 나가서도 안 된다.

요약은 revision이 READY가 된 뒤 background SUMMARIZE job이 만들고, detail은 저장된 것을
읽기만 한다. Rev N의 요약은 Rev N의 것이고, Rev N+1이 승격되어도 덮어쓰지 않는다.

요약 실패는 문서 실패가 아니다. `summary_status`는 생성 컬럼 `is_ready`에 포함되지 않으므로
요약이 없거나 실패해도 검색·다운로드는 영향을 받지 않는다.

### 21.2 Response

```text
summary: {
  "state": "PENDING" | "RUNNING" | "SUCCESS" | "FAILED" | "SKIPPED",
  "content": string | null,        // state=SUCCESS 일 때만
  "generated_at": timestamp | null,
  "available": boolean,            // capability
  "revision_id": uuid | null       // 이 요약이 설명하는 revision
}
```

`state`는 **이 revision에** 무슨 일이 있었는지이고, `available`은 **이 배포 환경이** 요약을
만들 수 있는지다. 둘은 다른 질문이고 사용자에게 해야 할 말도 다르다.

- `available: false` → "이 환경에서는 문서 요약을 생성하지 않습니다."
- `available: true`, `state: PENDING|RUNNING` → "생성하고 있습니다."

provider가 꺼진 환경에서 영원히 "생성 중"을 보여주는 것이 이 두 필드를 나눈 이유다.
`available`은 API의 capability 필드이며, DB의 `summary_status` CHECK에는 `DISABLED` 값을
**추가하지 않는다.** 배포 환경의 설정을 문서의 상태로 기록하면, 설정이 바뀌었을 때 과거
데이터가 거짓말이 된다.

### 21.3 두 개의 스위치 — provider와 승인은 별개다

문서 본문이 외부로 나가려면 **둘 다** 참이어야 한다.

```text
LLM_PROVIDER                     어떤 서비스에 닿을 수 있는가   (배포 작업)
DOCUMENT_EXTERNAL_LLM_ENABLED    이 corpus를 보내도 되는가      (승인)
```

서로를 함의하지 않고, **API key가 존재한다는 사실도 둘 중 어느 것도 함의하지 않는다.**
vendor가 설정되고 유효한 key가 꽂혀 있어도 두 번째 스위치가 false면 아무것도 나가지 않는다.

`DOCUMENT_EXTERNAL_LLM_ENABLED`의 default는 **false**이며, `1 / true / yes / on` 만 참으로
읽는다. unset·빈 값·오타를 포함한 그 외 전부는 false다. 여기서 관대한 파서의 실패
모드는 실제 사내 문서가 제3자에게 전송되는 것이다.

실제 공유폴더가 마운트된 환경에서는 false를 유지한다. synthetic / non-sensitive 문서만
있는 환경에서만 의도적으로 true로 켠다.

요약 생성과 document-scoped chat은 **같은 함수**(`document_generation_enabled()`)를 읽는다.
검사가 두 벌이면 언젠가 어긋나고, 그때 느슨한 쪽이 실질적인 정책이 된다.

### 21.4 노출하지 않는 것

`summary_provider`, `summary_model`, `summary_prompt_version`은 응답에 **없다.** 어떤 모델이
썼는지는 우리 파이프라인의 운영 사실이지 독자가 답변의 무게를 재는 근거가 아니고, 사내
문서를 어디로 보내는지 묻는 사람에게 그대로 답해 주는 필드이기도 하다.

점수·유사도·신뢰도는 v1과 동일하게 노출하지 않는다.

---

## 22. Chat capability와 비활성화 응답 (신규)

### 22.1 `chat.available`

`GET /api/v1/documents/{document_id}`가 `chat: { "available": boolean }`을 함께 준다.
`summary.available`과 **같은 두 스위치**를 읽는다.

이 필드가 필요한 이유는 하나다. 이것이 없으면 기능이 꺼져 있다는 사실을 알 수 있는
유일한 방법이 "질문을 입력하고 실패하는 것"인데, 그건 알게 되기 가장 나쁜 순간이다.

`available: false`일 때 프론트엔드는:

```text
"AI 질문 기능이 현재 비활성화되어 있습니다."  표시
composer / input / send 버튼 disabled
```

### 22.2 `FEATURE_UNAVAILABLE` (503)

`POST /chat/sessions/{session_id}/messages`가 생성 불가 상태에서 반환한다.

**500은 허용하지 않는다.** 설정으로 꺼진 기능은 서버 장애가 아니고, 500은 클라이언트에게
영원히 성공하지 않을 재시도를 권하는 응답이다. 503 + `FEATURE_UNAVAILABLE`은 "이 능력이
지금 없다"는 뜻이고, UI가 컨트롤을 비활성화하는 근거가 된다.

이 코드를 선언하는 route는 message route 하나뿐이다. session 생성·조회는 아무것도 생성하지
않으므로 provider 부재로 거절될 수 없다.

검사는 **retrieval보다 먼저** 일어난다. 기능이 꺼져 있으면 문서 본문을 메모리로 읽을
이유조차 없다.

---

## 23. (개정) Endpoint 요약

| Method | Path | 인증 | v1.2 변경 |
| --- | --- | --- | --- |
| `GET` | `/api/v1/search` | 필수 | — |
| `GET` | `/api/v1/documents/{document_id}` | 필수 | `summary`, `chat` 추가 |
| `GET` | `/api/v1/documents/{document_id}/revisions` | 필수 | — |
| `GET` | `/api/v1/documents/{document_id}/download` | 필수 | — |
| `GET` | `/api/v1/tags` | 필수 | — |
| `GET` | `/api/v1/departments` | 필수 | — |
| `GET` | `/api/v1/folders` | 필수 | — |
| `POST` | `/api/v1/chat/sessions` | 필수 | `document_id` 추가, `document_scope` 추가 |
| `GET` | `/api/v1/chat/sessions` | 필수 | `document_scope` 추가 |
| `GET` | `/api/v1/chat/sessions/{session_id}` | 필수 | `document_scope` 추가 |
| `POST` | `/api/v1/chat/sessions/{session_id}/messages` | 필수 | 요청 shape 변경 없음, `503` 추가 |

합계 11개. **신규 엔드포인트 없음.**

---

## 24. v1.2 Open Issues

### OI-8. 오래된 document-scoped session 찾기 — **미해결**

`GET /chat/sessions`에 document 필터가 없어서, 프론트엔드는 세션 목록 첫 페이지에서만
해당 문서의 기존 대화를 찾는다. 그보다 오래된 대화가 있으면 새 세션이 만들어진다.
동작은 정상이고 데이터도 새지 않지만 대화가 이어지지 않는다.

필터를 추가하면 엔드포인트 수는 그대로지만 query parameter가 늘어난다. 사용 빈도를 보고
결정한다.

### OI-9. 중간 요약은 저장하지 않는다 — **의도된 MVP 범위**

긴 문서는 recursive reduction으로 처리한다. 중간 요약은 저장하지 않으므로 재생성은
전체를 다시 계산한다. 저장하면 revision당 행이 늘고 ACL 대상이 하나 더 생기는데,
재생성 빈도가 그 비용을 정당화할 만큼 높은지 아직 모른다.

### OI-11. 호출 수는 문서 길이에 비례한다 — **하드 바운드의 대가**

한 번의 호출에 들어가는 원문은 `MAX_GROUP_CHARS`로 **어느 레벨에서도** 고정이다. 그룹이
많아졌다는 이유로 budget을 넓히지 않는다. 그래서 문서가 10배 길면 1차 호출도 대략 10배가
된다(축약 단계는 로그 스케일).

무한 비용을 막기 위해 revision당 총 호출 수 상한 `MAX_SUMMARY_CALLS = 200`을 둔다. 초과하면
일부만 요약해서 전체인 척 하지 않고 `SKIPPED` + `TOO_LARGE`로 기록한다. 재시도해도 결과가
같으므로 job은 FAILED가 아니라 SUCCESS로 닫는다.

### OI-10. 요약 품질 평가 — **synthetic corpus 한정**

요약 품질·비용·호출 수 측정은 synthetic / non-sensitive 문서로만 한다. 실제 사내 HWP
문서를 외부 LLM으로 보내는 것은 승인 전까지 금지다.
