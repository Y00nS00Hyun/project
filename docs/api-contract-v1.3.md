# 사내 문서 관리 시스템 — API Contract v1.3

**v1.2에 대한 additive 개정이다.** 이전 문서들(`v1`, `v1.1`, `v1.2`)은 그대로 두고 이 문서가
그 위의 차이만 기술한다. 기존 엔드포인트의 요청·응답 스키마는 변경되지 않으므로 v1.2
클라이언트는 수정 없이 계속 동작한다.

```text
기준 문서   docs/api-contract-v1.md, v1.1.md, v1.2.md   (변경하지 않음)
개정 성격   additive only
method/path 조합   11 → 20
```

```text
신규   POST /api/v1/auth/signup
신규   POST /api/v1/auth/login
신규   POST /api/v1/auth/logout
신규   GET  /api/v1/auth/me
신규   GET  /api/v1/auth/capability
신규   POST /api/v1/auth/password
신규   POST /api/v1/auth/password/reset
신규   GET  /api/v1/admin/users
신규   POST /api/v1/admin/users/{user_id}/approve
신규   POST /api/v1/admin/users/{user_id}/disable
신규   POST /api/v1/admin/users/{user_id}/admin
```

**부서(department) 개념은 인증 어디에도 없다.** 회사가 부서 구분을 사용하지 않으므로
가입·승인·사용자 표시 어느 것도 부서를 다루지 않는다. `departments` 테이블,
`document_permissions` 의 DEPARTMENT principal, 기존 `GET /departments` 는 그대로 둔다 --
아직 부서 단위로 부여된 문서가 있을 수 있고, 그것을 정리하는 일은 인증 단순화와 별개다.
Local Auth 가 거기에 **의존하지 않는다**는 것이 이번 작업의 요점이다.

---

## 25. 전제 — Local Auth가 운영 인증이다

회사에 SSO / OIDC / SAML / LDAP / AD 기반 통합 로그인이 **없다.** 따라서 이 인증은 임시
개발용 대체물이 아니라 이 시스템의 실제 운영 인증 방식이다. production에서도 동작한다.

향후 다른 인증 방식으로 교체할 가능성은 하나의 방식으로 대비한다 — **인증이 성공하면 그
뒤로는 user_id 하나만 넘어간다.**

```text
인증 (무엇이든)  ──►  user_id  ──►  users ──► department ──► document_permissions
                                              └──► READ_ACL_PREDICATE
```

인증 계층은 권한 로직을 **복제하지 않는다.** `require_user`가 하는 일은 "당신이 누구인가"를
정하는 것뿐이고, "무엇을 읽어도 되는가"에는 아무 말도 하지 않는다. 다른 인증 방식은
user_id만 만들어내면 된다.

### 25.1 두 개의 스위치, 둘 다 기본 false

```text
LOCAL_AUTH_ENABLED     비밀번호 로그인을 제공하는가
SELF_SIGNUP_ENABLED    새 계정 생성을 허용하는가
```

`APP_ENV`나 credential 존재만으로 자동 활성화되지 **않는다.** 운영자가 명시적으로 켜야 한다.

`SELF_SIGNUP_ENABLED`가 별도 스위치인 이유는, 고정된 계정 집합만 운영하면서 신규 가입은
막는 환경이 정상적인 선택지이기 때문이다.

### 25.2 debug identity header

`X-Debug-User-Id`는 **production에서 계속 금지**된다. 자동화 테스트용이며, local auth 설정과
무관하게 production에서는 무시된다.

인증 우선순위:

```text
1. local auth session cookie   (모든 환경)
2. debug identity header       (non-production 에서만)
```

cookie를 먼저 보는 이유는 그것이 실제 사람이 가진 것이기 때문이다. 스크립트에 남아 있는
오래된 header가 방금 로그인한 세션을 덮어써서는 안 된다.

---

## 26. 계정 수명주기

```text
signup  ──►  PENDING  ──(관리자 승인)──►  ACTIVE  ──(관리자 비활성화)──►  DISABLED
```

승인은 **로그인 허용만을 의미한다.** 문서 열람 권한은 부여하지 않으며, 그것은
`document_permissions` 에서 별도로 결정된다 -- 승인과 권한 부여가 한 동작이면 "이 사람을
들여보낸다"와 "이 사람에게 무엇을 보여준다"가 구분되지 않는다.

**가입과 접근 허용은 같은 행위가 아니다.** 사내 문서 시스템의 운영 로그인이므로, 가입
화면에 닿을 수 있는 사람이 곧바로 계정을 만들어 문서를 볼 수 있으면 안 된다.

`users.status`는 기존 `users.is_active`와 **별개 컬럼**이다. `is_active`는 "이 계정이 동작해야
하는가"이고 이미 ACL 조인에서 그 의미로 쓰인다. `status`는 수명주기의 어디인지이며, PENDING은
비활성의 한 종류가 아니라 **사람의 판단을 기다리는 상태**다. 질문이 둘이므로 컬럼도 둘이다.

`status`는 로그인 시점뿐 아니라 **매 요청의 세션 확인에서도** 검사한다. 권한을 회수하면 그
사람의 살아 있는 세션도 함께 끝나야지, 만료될 때까지 7일을 기다리면 안 된다.

---

## 27. 인증 API

### 27.1 `GET /auth/capability` — 인증 불필요

```json
{ "local_auth_enabled": true, "signup_enabled": true }
```

로그인 화면은 아무도 로그인하기 전에 이걸 물어야 하므로 인증을 요구하지 않는다. 프론트엔드가
자기 빌드 모드로 추측하지 않고 **서버에 묻는** 이유는 스위치가 서버에 있기 때문이다.

### 27.2 `POST /auth/signup`

```text
요청   { login_id, name, password, password_confirm }
응답   201 { "status": "PENDING", "message": "관리자 승인 대기 중입니다." }
```

**세션을 발급하지 않는다.** 계정이 PENDING이므로 세션을 주면 모든 보호된 route가 곧바로
거절하는 세션을 쥐여주는 셈이고, 가입과 승인의 구분이 흐려진다.

요청 본문은 위 네 필드가 전부다 (`extra='forbid'`). 다음은 **존재하지 않는다**:

```text
department_id            회사가 부서 구분을 사용하지 않는다
role / is_system_admin   가입으로 관리자가 될 수 없다
document permission      가입은 어떤 문서 권한도 부여하지 않는다
```

막는 방식이 검사가 아니라 **구조**라는 점이 중요하다. 보낼 수 있는 모양 자체가 없다.

`login_id`는 소문자로 정규화되어 저장된다. `Alice`와 `alice`가 두 계정이 되면 한 사람이 다른
사람 아이디의 근사치를 등록할 수 있다.

가입에서만 "이미 사용 중인 아이디입니다"를 말한다. 로그인은 아래처럼 통일된 메시지를
쓰므로, 이것이 노출하는 것은 아이디의 존재뿐이고 그 이상이 아니다. 대안은 아무도 완성할 수
없는 양식이다.

### 27.3 `POST /auth/login`

```text
요청   { login_id, password }
성공   200 { user_id, name, department_name, is_system_admin } + Set-Cookie
```

실패 응답:

| 상황 | 코드 | 메시지 |
| --- | --- | --- |
| 비밀번호 불일치 | 401 `UNAUTHENTICATED` | 아이디 또는 비밀번호가 올바르지 않습니다. |
| 존재하지 않는 계정 | 401 `UNAUTHENTICATED` | (동일) |
| 잠긴 계정 | 401 `UNAUTHENTICATED` | (동일) |
| 승인 대기 | 403 `FORBIDDEN` | 관리자 승인 대기 중입니다. |
| 비활성화됨 | 403 `FORBIDDEN` | 비활성화된 계정입니다. 관리자에게 문의해 주세요. |
| 요청 과다 | 429 `RATE_LIMITED` | 요청이 많습니다. 잠시 후 다시 시도해 주세요. |
| 기능 꺼짐 | 503 `FEATURE_UNAVAILABLE` | 로컬 로그인이 비활성화되어 있습니다. |

**승인 대기 / 비활성화 메시지는 비밀번호 검증에 성공한 뒤에만 나온다.** 순서가 반대라면 그
메시지가 곧 "이 아이디가 존재한다"는 신탁이 된다. 이렇게 하면 계정이 승인 대기라는 사실을
알 수 있는 사람은 이미 그 비밀번호를 아는 사람뿐이다.

존재하지 않는 계정에 대해서도 버리는 해시를 한 번 계산한다. 즉시 반환하면 응답 시간 차이로
아이디 존재 여부가 드러난다.

### 27.4 `POST /auth/logout`

```text
204, 세션 revoke + cookie 삭제
```

cookie만 지우지 않는다. token 사본을 보관한 사람에게 여전히 유효한 세션이 남기 때문이다.

### 27.5 `GET /auth/me`

```json
{ "user_id", "name", "is_system_admin" }
```

email, login_id, 세션 정보, 권한 목록, 부서는 **없다.** 화면이 필요로 하는 것은 인사할 이름과 표시할
부서이고, 그 이상은 이 엔드포인트가 슬그머니 사용자 디렉터리가 되는 일이다.

`is_system_admin`은 UI가 관리자 메뉴를 보여줄지 정하기 위한 것이며, **권한의 근거가 아니다.**
권한은 모든 admin route가 서버에서 직접 확인한다.

---

## 28. 세션

```text
저장 위치   auth_sessions (server-side)
브라우저    HttpOnly cookie, random token only
DB          token 원문이 아니라 SHA-256 hash
만료        기본 7일 (LOCAL_AUTH_SESSION_DAYS)
```

cookie 속성:

```text
HttpOnly   true          스크립트가 읽을 수 없으므로 XSS로 세션을 빼낼 수 없다
SameSite   Lax           cross-site POST에 함께 실려가지 않는다
Secure     AUTH_COOKIE_SECURE   HTTPS면 true, 평문 HTTP면 false
Path       /
```

`Secure`를 요청에서 추측하지 않고 설정으로 받는 이유는, 요청은 클라이언트가 통제하는
것이기 때문이다. 평문 HTTP에서 `Secure` cookie는 아예 저장되지 않으므로 개발 환경은 false다.

**localStorage를 쓰지 않는다.** token은 HttpOnly cookie로만 존재하므로 프론트엔드 코드가
저장할 것도, 주입된 스크립트가 읽어낼 것도 없다.

DB에 hash만 두는 이유: DB를 읽을 수 있게 된 사람이 그것만으로 남의 세션을 가장할 수 없어야
한다. 비밀번호 해시가 아니라 평범한 SHA-256인 것은 token이 이미 256비트 난수라 무차별
대입할 것이 없고, 매 요청마다 memory-hard KDF 비용을 낼 이유가 없기 때문이다.

---

## 29. 비밀번호

```text
알고리즘   Argon2id (argon2-cffi 기본 파라미터)
저장       encoded hash 문자열만. 평문은 어떤 컬럼에도 없다
길이       8 ~ 256자
```

파라미터를 손으로 고르지 않는 이유는 오늘의 하드웨어 추정을 코드에 얼려두는 일이기
때문이다. 라이브러리가 시간에 따라 올리고, 파라미터가 각 해시 안에 들어 있으므로 바뀐 뒤에도
기존 해시가 계속 검증된다. 로그인 시 `check_needs_rehash`로 조용히 재계산한다.

brute-force 완화는 두 겹이다.

```text
계정별   10회 실패 → 60초 잠금        (한 계정을 노리는 공격)
주소별   60초 내 20회 → 429           (여러 계정에 뿌리는 공격)
```

계정별 잠금은 아이디를 바꿔가며 시도하는 공격을 보지 못하고, 주소별 제한은 여러 주소로
분산된 공격을 보지 못한다. 둘을 함께 두면 각각의 값싼 버전을 막는다. 어느 쪽도 엣지의 진짜
rate limiter를 대체하지 않으며 그런 척하지도 않는다.

주소는 socket peer를 쓰고 `X-Forwarded-For`는 **신뢰하지 않는다.** 클라이언트가 설정할 수
있는 헤더이므로, 존중하면 문자열 하나 바꿔서 자기 카운터를 초기화할 수 있다.

로그에 비밀번호·해시·session token을 출력하지 않는다. 남는 식별자는 user_id뿐이며 이는 이미
다른 모든 로그에 있는 값이다.

---

## 30. 관리자

### 30.1 `users.is_system_admin` 은 문서 ADMIN 권한과 다른 개념이다

```text
document_permissions.permission = 'ADMIN'   문서 한 건에 대한 권한
                                            READ_ACL_PREDICATE가 이미 읽기로 인정한다
users.is_system_admin                       계정 관리 권한
```

섞지 않는다. 섞으면 문서 하나에 ADMIN을 받은 사람이 시스템 전체를 넘겨받는다.

### 30.2 첫 관리자

HTTP로 만들 수 없다. 공개 가입 양식이 관리자를 찍어내면 그 자체가 시스템 탈취 경로다.
서버 접근 권한이 있는 사람이 CLI로 첫 관리자를 만들고, 이후는 화면에서 관리자가 만든다.

```bash
docker compose exec backend python -m auth grant-admin --login-id <아이디>
```

### 30.3 관리 API

```text
GET  /admin/users[?status=PENDING]      승인 대기 목록 포함
POST /admin/users/{id}/approve          본문 없음
POST /admin/users/{id}/disable
POST /admin/users/{id}/admin            { granted }
```

`approve` 에 요청 본문이 없는 이유: 승인은 "이 사람이 로그인해도 되는가" 하나의 결정이고,
본문이 있으면 거기에 두 번째 결정이 끼어들 자리가 생긴다.

전부 `require_admin_user`를 거친다. 일반 사용자는 **403**, 비로그인은 **401**이다.

응답에 password hash, session, token은 **없다.** `login_id`는 관리자가 승인 요청한 사람과
대조하기 위해 필요하므로 포함된다.

관리자는 자기 자신을 비활성화할 수 없다. 마지막 관리자가 스스로를 잠그는 일은 나중에
발견할 성질의 것이 아니다.

### 30.4 승인이 부여하는 것

**승인은 어떤 문서 권한도 부여하지 않는다.** status를 ACTIVE로 바꾸는 것이 전부다.
승인만 된 계정은 문서 0건을 본다 -- default deny 가 그대로 유지된다.

문서 읽기 권한은 별개의 의도적인 작업이다.

```bash
# 신규 문서는 DOCUMENT_DEFAULT_ACCESS 에 따라 수집 시점에 자동 부여된다.
# 기존 문서에 소급 적용:
docker compose exec backend python -m auth grant-public-read --all-current-documents

# 특정 사용자에게만:
docker compose exec backend python -m auth grant-user-read \
    --login-id <id> --all-current-documents
```

어느 쪽도 ACL 변경이 아니다. predicate 가 찾는 행을 쓸 뿐이고, soft delete 된 문서는
건너뛴다.

### 30.5 신규 사용자의 문서 권한 — 운영 정책

이 시스템은 **일반 사내 문서만 수집한다.** 기밀·인사·급여·특정 사용자 전용 문서는 읽기
시점에 걸러내는 것이 아니라 수집 대상에서 제외한다. 그래서 승인된 계정은 등록된 모든
문서를 읽는다.

이것을 "ACL 이 항상 참을 반환한다" 로 구현하지 않았다. `document_permissions` 에
**principal 을 하나 추가**했다 (`is_public`, migration 0006).

```text
principal        행 수                 뜻
user_id          사용자 x 문서         이 사람에게
department_id    부서 x 문서           이 부서에게 (기존 문서용, 인증은 안 씀)
is_public        문서 하나당 하나      승인된 계정 전원에게
```

바뀌지 않은 것:

```text
default deny          권한 행이 없는 문서는 아무도 못 읽는다
ACL before retrieval  같은 predicate 가 후보 CTE 에서 그대로 실행된다
개별 권한             문서별·사용자별 grant 는 그대로 동작한다
익명 접근 없음        "전원" 은 "승인된 계정 전원" 이다
```

마지막 항목은 관례가 아니라 SQL 이다. 공개 분기도 `u.is_active` 와 `u.status='ACTIVE'` 를
함께 확인하므로, 사용자가 아닌 id 는 공개 문서도 통과하지 못한다. `require_user` 가 이미
막지만 ACL 이 session 계층을 신뢰하는 대신 스스로 확인한다.

문서 하나를 비공개로 되돌리는 것은 그 행을 지우는 일이고, 그 뒤에는 개별 grant 만
적용된다. 코드 변경도 schema 변경도 필요 없다 -- 그것이 principal 을 늘린 이유다.

**권한 관리 UI 는 이번 범위가 아니다.** 위 조작은 CLI 로 한다.

---

## 31. (개정) Endpoint 요약

| Method | Path | 인증 | v1.3 |
| --- | --- | --- | --- |
| `GET` | `/api/v1/auth/capability` | 불필요 | **신규** |
| `POST` | `/api/v1/auth/signup` | 불필요 | **신규** |
| `POST` | `/api/v1/auth/login` | 불필요 | **신규** |
| `POST` | `/api/v1/auth/logout` | 불필요 | **신규** |
| `GET` | `/api/v1/auth/me` | 필수 | **신규** |
| `POST` | `/api/v1/auth/password` | 필수 | **신규** |
| `POST` | `/api/v1/auth/password/reset` | 불필요 | **신규** |
| `GET` | `/api/v1/admin/users` | 관리자 | **신규** |
| `POST` | `/api/v1/admin/users/{user_id}/approve` | 관리자 | **신규** |
| `POST` | `/api/v1/admin/users/{user_id}/disable` | 관리자 | **신규** |
| `POST` | `/api/v1/admin/users/{user_id}/admin` | 관리자 | **신규** |
| | *(v1.2까지의 11개는 변경 없음)* | | — |

합계 22개.

---

## 32. v1.3 Open Issues

### OI-12. 비밀번호 재설정 없음 — **미구현**

잊어버린 비밀번호를 사용자가 스스로 되돌릴 방법이 없다. 메일 발송 경로가 없으므로 관리자가
계정을 비활성화하고 다시 만드는 것이 현재의 유일한 절차다. 메일 인프라가 생기면 재검토한다.

### OI-13. 문서 권한 부여 화면 없음 — **의도된 MVP 범위**

관리 UI는 승인·비활성화·관리자 권한만 다룬다. 문서 읽기 권한 부여는 CLI다.
권한 관리 화면은 그 자체로 별도 과제이며, 잘못 만들면 ACL을 넓히는 가장 쉬운 길이 된다.

### OI-15. departments 정리 — **별도 과제**

`departments` 테이블, `users.department_id`, `document_permissions` 의 DEPARTMENT
principal, `GET /departments` 는 남아 있다. Local Auth 는 어느 것에도 의존하지 않지만,
실제로 필요 없다고 확정되면 별도 cleanup migration 으로 다룬다. 지금 지우면 기존 ACL
동작을 건드려 regression 을 만들 이유가 없다.

### OI-14. rate limiting이 프로세스 로컬 — **알려진 한계**

주소별 카운터는 워커 프로세스마다 따로이고 재시작하면 초기화된다. 의존성도 왕복도 없이
얻을 수 있는 정직한 범위이며, 값싼 공격은 실제로 막는다. 더 강한 것은 엣지에 둔다.
