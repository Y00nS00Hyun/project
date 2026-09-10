# 사내 문서 관리 시스템 — API Contract v1.1

**v1에 대한 additive 개정이다.** `docs/api-contract-v1.md`는 그대로 두고 이 문서가 그 위의 차이만 기술한다.
v1 클라이언트는 수정 없이 계속 동작한다.

```text
기준 문서   docs/api-contract-v1.md   (변경하지 않음)
개정 성격   additive only
method/path 조합   10 → 11
```

v1에서 **바뀐 것은 없다.** 기존 엔드포인트의 요청·응답 스키마, 오류 코드 집합, 공통 envelope,
페이지네이션 규약은 전부 v1 그대로다. 이 문서는 다음 둘만 추가한다.

```text
신규   GET /api/v1/folders
추가   GET /api/v1/search 의 folder_path query parameter
```

DB 스키마는 변경하지 않는다. 폴더 구조는 `documents.source_path`에서 파생하며 folders 테이블은 없다.

---

## 12. Folders API (신규)

```http
GET /api/v1/folders
```

공유폴더의 구조를 **현재 사용자가 볼 수 있는 범위만큼** 반환한다.

### 12.1 성격 — navigation, facet 아님

폴더 트리는 검색 facet이 아니라 탐색 UI다. 트리는 다음 기준으로만 구성한다.

```text
ACL(READ 권한)
+ is_deleted = FALSE
+ current revision 이 READY
```

`year`, `tag_id`, `file_type`, `q`는 **트리를 바꾸지 않는다.** 매뉴얼만 들어 있는 폴더는
사용자가 "보고서" 필터를 걸어도 사라지지 않고, 선택했을 때 검색 결과가 0건이면 된다.
필터에 따라 폴더가 나타났다 사라지면 탐색 구조로 신뢰할 수 없기 때문이다.

이 엔드포인트는 **query parameter를 받지 않는다.** 정의되지 않은 parameter는 무시하지 않고
`422 VALIDATION_ERROR`로 거절한다.

### 12.2 ACL — 검색 이전에 적용

권한 없는 문서는 폴더 후보 집합에도 들어가지 않는다. 전체 트리를 만든 뒤 클라이언트에서
숨기는 방식은 **금지한다.**

```text
① READ 가능한 current READY document 를 먼저 선정
② 그 document 들의 source_path 에서만 조상 폴더를 전개
```

따라서 **접근 가능한 문서가 하나도 없는 폴더는 이름조차 응답에 나타나지 않는다.**
비공개 프로젝트의 이름 자체가 정보이기 때문이다.

### 12.3 Response

```json
{
  "items": [
    {
      "path": "프로젝트_A",
      "name": "프로젝트_A",
      "parent_path": null,
      "depth": 1,
      "document_count": 3
    },
    {
      "path": "프로젝트_A/요구사항",
      "name": "요구사항",
      "parent_path": "프로젝트_A",
      "depth": 2,
      "document_count": 2
    },
    {
      "path": "%C7%C1%B7%CE%C1%A7Ʈ_B",
      "name": "프로젝트_B",
      "parent_path": null,
      "depth": 1,
      "document_count": 2
    }
  ]
}
```

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `path` | string | **canonical relative path.** 폴더의 식별값 |
| `name` | string | 마지막 segment의 **표시용** 이름 |
| `parent_path` | string \| null | 부모의 canonical path. 최상위는 `null` |
| `depth` | int | 1부터 시작 |
| `document_count` | int | 해당 폴더 **하위 전체**에서 사용자가 읽을 수 있는 문서 수 |

pagination이 없다. 트리는 탐색이고 잘린 트리는 탐색할 수 없다.

### 12.4 `path` 와 `name` 은 서로 대체할 수 없다

세 번째 예시가 그 이유다. `path`는 `%C7%C1%B7%CE%C1%A7Ʈ_B`이고 `name`은 `프로젝트_B`다.
Windows에서 만들어진 한글 폴더는 CP949 바이트로 저장되어 있고, 그 바이트를 UTF-8 컬럼에
담기 위해 canonical 형태로 이스케이프하기 때문이다.

```text
path   filesystem 왕복과 검색 필터링에 쓰는 식별값
name   화면 표시 전용
```

**클라이언트는 `name`을 이어붙여 경로를 만들면 안 된다.** 서버가 준 `path`를 그대로 보관했다가
`folder_path`로 되돌려 보낸다. 이름을 이어 붙이면 어떤 문서와도 매칭되지 않는 경로가 나온다.

절대경로는 어떤 필드에도 나타나지 않는다. 모든 값은 공유폴더 root 기준 상대경로이며,
클라이언트는 root의 실제 위치를 알 수 없다.

---

## 6.1 (개정) Search query parameters

v1 표에 다음 한 행을 추가한다. 나머지는 변경 없다.

| 이름 | 타입 | 필수 | 기본 | 설명 |
| --- | --- | --- | --- | --- |
| `folder_path` | string | 아니오 | — | 공유폴더 기준 상대 경로. **해당 폴더의 하위 전체**가 대상 |

`GET /api/v1/folders`가 반환한 `path`를 그대로 보낸다.

### 6.1.1 조합

기존 필터와 **AND**로 결합한다.

```text
folder_path + year + tag_id + file_type + q
```

### 6.1.2 경계

subtree 판정은 **구분자까지 포함한 prefix**로 한다.

```text
folder_path = "프로젝트_A"
  매칭     프로젝트_A/요구사항/명세.hwp
  비매칭   프로젝트_A2/다른문서.hwp      ← 구분자가 없으면 잘못 매칭된다
```

canonical path에는 `%`가 바이트 이스케이프로 실제 존재하고 `_`는 파일명에 흔하다.
둘 다 SQL LIKE의 와일드카드이므로 서버가 `ESCAPE '\'` 규약으로 literal 처리한다.
이스케이프하지 않으면 `%C7%C1`이 사실상 모든 경로에 매칭된다.

### 6.1.3 검증

서버는 `folder_path`를 정규화하지 않고 **거절**한다. 수정해야 하는 경로는 `/folders`가 준 것이
아니며, 조용히 재해석하면 사용자가 요청하지 않은 곳을 검색하게 된다.

```text
거절   절대경로 (/ 로 시작)
거절   '..' 또는 '.' segment
거절   빈 segment (a//b)
거절   '\' 구분자
거절   canonical 형식이 아닌 값 (%ZZ 등)
→ 422 VALIDATION_ERROR
```

`%2E%2E`는 이스케이프가 아니라 문자 그대로이므로 `..`가 되지 않는다.
canonical 규약에서 이스케이프로 인정하는 값은 `%25`와 `%80`~`%FF`뿐이다.

---

## 15. (개정) Endpoint 요약

| Method | Path | 인증 | 변경 |
| --- | --- | --- | --- |
| `GET` | `/api/v1/search` | 필수 | `folder_path` 추가 |
| `GET` | `/api/v1/documents/{document_id}` | 필수 | — |
| `GET` | `/api/v1/documents/{document_id}/revisions` | 필수 | — |
| `GET` | `/api/v1/documents/{document_id}/download` | 필수 | — |
| `GET` | `/api/v1/tags` | 필수 | — |
| `GET` | `/api/v1/departments` | 필수 | — |
| `GET` | `/api/v1/folders` | 필수 | **신규** |
| `POST` | `/api/v1/chat/sessions` | 필수 | — |
| `GET` | `/api/v1/chat/sessions` | 필수 | — |
| `GET` | `/api/v1/chat/sessions/{session_id}` | 필수 | — |
| `POST` | `/api/v1/chat/sessions/{session_id}/messages` | 필수 | — |

합계 11개.

---

## 17. v1.1 Open Issues

### OI-5. 최상위 폴더가 프로젝트인가 — **미확정**

회사가 공유폴더의 최상위 폴더를 프로젝트 단위로 운영하는지 아직 확인되지 않았다.
**코드는 이를 강제하지 않는다.** 모든 노드는 일반 folder이며, API에 `project` 개념은 없다.
규칙이 확인되면 `depth = 1`을 프로젝트로 해석하는 계층을 UI에 올릴 수 있다.

### OI-6. 이동/rename 감지 — **별도 과제**

`source_path`가 문서의 정체성이므로 파일을 다른 폴더로 옮기면 **새 document가 생기고**
옛 경로는 missing → soft-delete 된다. document_id가 바뀌므로 revision 이력과 chat citation은
옛 문서에 남는다.

폴더 트리 UI는 사용자가 공유폴더에서 파일을 옮기도록 유도하므로 이 문제가 표면화된다.
`content_hash`가 같다는 이유만으로 이동으로 판정하지는 않는다 — 동일 내용의 사본이
존재할 수 있기 때문이다. 별도 설계 과제로 남긴다.

### OI-7. 빈 폴더는 표현할 수 없다

트리를 `source_path`에서 파생하므로 문서가 하나도 없는 폴더는 흔적이 없다.
이번 요구사항(접근 가능 문서 기준 트리)과는 부합하지만, "폴더는 있는데 안 보인다"는
문의가 나올 수 있다.
