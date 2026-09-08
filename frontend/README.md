# React Search UI v1

사내 문서 검색과 원본 열람용 UI. 실제 FastAPI `/api/v1`에 연결한다.
API 기준: [HTTP 구현 문서](../docs/http-api-search-documents.md),
[계약](../docs/api-contract-v1.md), `src/api/schemas/` 및 실제 OpenAPI.

## 설치 / 실행

Node.js 22.12 이상 LTS 권장. 이 작업에서는 Node.js 24.20.0으로 검증했다.

```bash
cd frontend
npm ci
cp .env.example .env.local
```

`.env.local`의 `VITE_DEBUG_USER_ID`를 개발 DB에 존재하는 활성 사용자 `users.id`로 설정한다.
사용자에게 문서 READ 권한 또는 소속 부서 READ 권한이 있어야 문서가 검색된다.
이 값은 개발 설정이며 일반 UI에는 사용자 ID 입력 기능이 없다.

Backend는 저장소 루트에서 실행한다. 기존 migration / ingestion / embedding이 완료된 DB,
원본 공유폴더, 로컬 임베딩 모델 캐시를 사용한다.

```bash
# 아직 API 의존성을 설치하지 않은 경우
.venv/bin/python -m pip install -e '.[api,db]'

export DATABASE_URL='postgresql://user:pass@localhost:5432/dbname'
export SHARED_ROOT='/실제/공유폴더'
export APP_ENV=development
export EMBEDDING_CACHE_DIR='/로컬/모델/캐시/hub'
PYTHONPATH=src .venv/bin/python -m uvicorn api.app:app --host 127.0.0.1 --port 8000
```

`DATABASE_URL`, `SHARED_ROOT`, 모델 캐시는 실제 환경에 맞게 바꾼다.
Semantic 검색은 기존 `sentence-transformers` 의존성과 로컬
`intfloat/multilingual-e5-small` 모델이 필요하다. 준비 방법은
[Embedding 문서](../docs/embedding-pipeline.md)를 따른다.
모델을 사용할 수 없으면 검색 오류를 표시한다. Browse / 상세 / 다운로드는 모델 없이 동작한다.

별도 터미널에서:

```bash
cd frontend
npm run dev
```

브라우저: **http://localhost:5173/search**.
포트가 사용 중이면 조용히 다른 포트로 이동하지 않고 실패한다.

## 환경변수 / Backend 연결

| 변수 | 기본값 / 의미 |
| --- | --- |
| `VITE_API_BASE_URL` | `/api/v1`. API prefix까지 포함 |
| `VITE_DEV_PROXY_TARGET` | `http://127.0.0.1:8000`. 개발 프록시 대상 |
| `VITE_DEV_PORT` | `5173` |
| `VITE_DEBUG_USER_ID` | 미설정. `.env.local`에서만 개발 identity 설정 |

기본 개발 연결은 `/api`를 Vite가 프록시하여 동일 origin으로 사용한다.
API를 다른 origin으로 직접 호출한다면 Backend의 `CORS_ALLOW_ORIGINS`에
정확한 Frontend origin을 설정한다. 와일드카드 허용은 사용하지 않는다.
직접 연결에서 `Content-Disposition`을 읽을 수 없는 경우 다운로드 이름은 문서 제목과 형식으로 대체한다.

Production에서는 `import.meta.env.DEV` 분기가 제거되어 debug identity를 보내지 않는다.
Backend SSO는 아직 미구현이므로 현재 production 인증 응답은 401이다.
정적 배포는 `/search`, `/documents/*` 요청을 `index.html`로 연결하고 `/api`를 Backend로 연결해야 한다.
Vite 개발 프록시는 production bundle에 포함되지 않는다.

## Test / Build

```bash
cd frontend
npm test
npm run typecheck
npm run build
```

`npm run test:watch`로 테스트 감시, `npm run preview`로 빌드된 정적 화면을 확인한다.
Preview는 production bundle을 사용하므로 개발 identity 인증 데모는 `npm run dev`를 사용한다.

실제 개발 API HTTP smoke (Backend와 Vite 실행 후, 저장소 루트):

```bash
VITE_DEBUG_USER_ID='개발 DB의 활성 사용자 UUID' \
SMOKE_QUERY='접근 가능한 다운로드 문서의 검색어' \
python3 frontend/scripts/smoke.py
```

`SMOKE_API_BASE_URL` 기본값은 `http://localhost:5173/api/v1`이다.
Smoke는 데이터를 변경하지 않으며 검색 결과 첫 문서의 실제 다운로드까지 검사한다.
원본이 존재하는 READY 문서와 해당 문서의 READ 권한이 필요하다.

## Files Created / Modified

모든 추가·수정은 `frontend/` 내부에 있다. Backend 코드·계약은 변경하지 않았으며 자동 commit하지 않았다.

```text
frontend/
├── .env.example / .gitignore
├── package.json / package-lock.json
├── index.html / vite.config.ts / tsconfig*.json
├── README.md
├── scripts/smoke.py
└── src/
    ├── main.tsx / App.tsx / App.test.tsx
    ├── api/          client, types, search, documents, metadata 및 테스트
    ├── components/   SearchForm, Filters, ResultCard, Pagination,
    │                RevisionList, StateViews 및 테스트
    ├── hooks/        useSearchState, useAsyncResource 및 테스트
    ├── pages/        SearchPage, DocumentPage 및 테스트
    ├── test/         테스트 설정과 fixture
    └── labels.ts / labels.test.ts / styles.css / vite-env.d.ts
```

`node_modules/`, `dist/`, `.env.local`은 Git에서 제외된다.

## Frontend Stack

설치된 lockfile 기준: React / React DOM **19.2.8**, TypeScript **5.9.3**,
Vite **7.3.6**, React Router DOM **7.18.3**.
추가 runtime 라이브러리는 Router뿐이며 스타일은 기본 CSS다.
테스트는 Vitest 3.2.7, Testing Library, jsdom을 사용한다.

## Routes / Search UI

`/` → `/search`, 상세는 `/documents/:documentId`.
검색은 Enter / 검색 버튼으로 실행하며 타이핑만으로 API를 호출하지 않는다.
검색어가 비면 `q`를 생략해 최근 접근 가능 문서를 표시한다.
`mode`는 생략하여 서버의 계약 기본값 `default`를 사용한다.

부서·태그는 API에서 가져오며 태그는 전체 페이지를 순회한다.
연도는 최근 10년과 URL에 선택된 연도를 제공한다.
파일 형식은 `hwp`, `hwpx`, `docx`, `pdf`만 요청한다.
복수 태그는 반복 `tag_id`로 전달되어 AND 조건으로 검색한다.

검색어·필터·페이지는 URL에 저장된다. 조건 변경 시 1페이지로 돌아가며,
새로고침·뒤로가기·상세의 검색 복귀 링크에서 조건을 유지한다.
페이지당 기본 20건, 문서 단위 `total/page/size`로 페이지를 구성한다.
Snippet과 paragraph/page anchor는 실제 응답이 있을 때만 표시한다.
새 요청 중 이전 결과를 숨기고 AbortController와 요청 순서 검사로 오래된 응답을 무시한다.

## Document UI

문서명·부서·형식·태그·등록/수정일·현재 검색 버전·최신 파일 버전을 표시한다.
리비전은 별도 페이지 이동을 지원하며 처리 상태와 파싱 결과를 구분해 한국어로 표시한다.
원본은 document ID만 전달하는 fetch 다운로드로 받는다. 응답의 표시용 파일명을 사용하고,
다운로드 중 이탈하면 요청을 취소한다. 실패는 공통 오류 UI로 표시한다.

## API Integration / Authentication / Error Handling / Security

연결 endpoint: `GET /api/v1/search`, `/documents/{id}`,
`/documents/{id}/revisions`, `/documents/{id}/download`, `/tags`, `/departments`.

401은 “로그인이 필요합니다.”, 문서 404는 “문서를 찾을 수 없습니다.”로 표시한다.
422 / 409 / 500 및 기타 API 오류는 `error.message`와 문제 신고용 `request_id`를 표시한다.
네트워크 오류·비 JSON 오류는 일반 메시지를 사용하고 raw body / stack trace를 노출하지 않는다.
필터 목록 API 실패도 화면에 표시한다. 같은 검색어 재제출로 검색 실패를 재시도할 수 있다.

Score leak: 없음. Filesystem path 구성·표시: 없음. Client identity 입력 UI: 없음.
Production bundle의 debug identity 값 및 `X-Debug-User-Id` 문자열 부재를 확인했다.
`Anchor`는 discriminated union이며 응답에 없는 페이지·연도·유사도를 만들지 않는다.
RAG / Chat / 관리자 / 문서 수정·삭제 / SSO 구현은 포함하지 않았다.

## Tests

2026-09-08 검증:

- Frontend component / unit / routing: **81 tests PASS**.
- TypeScript 검사 + production build: **PASS**.
- 실제 PostgreSQL 기반 API 계약 / HTTP 테스트: **81 tests PASS**. 로컬 E5 모델 검색 포함.
- Vite → 실제 FastAPI → PostgreSQL / 로컬 E5: browse → default 검색 → 상세 →
  리비전 → 다운로드, 메타데이터, 401/404/422 **PASS**.
- 네 가지 필터 조합, 빈 browse 결과, SPA 직접 URL, 다운로드 원본 바이트 일치 **PASS**.
- 브라우저 자동화 도구는 도입하지 않았다. 화면 동작은 jsdom component 테스트,
  실제 서버 연결은 HTTP smoke로 검증했다.

## Live Demo

현재 작업 환경에는 별도의 임시 DB, 로컬 E5 모델, 다운로드 가능한 합성 DOCX 한 개를 준비했다.
문서 제목은 “서버 장애 대응 매뉴얼”이며 현재 버전과 처리 실패한 최신 버전을 비교할 수 있다.
Frontend `.env.local`은 이 임시 DB의 개발 사용자로 설정되어 있다.
이 데모 데이터와 런처는 `/tmp/react-search-ui-demo/`에만 있으며 업무 DB와 분리되어 있다.

현재 환경에서 재실행:

```bash
# 저장소 루트, 터미널 1
.venv/bin/python /tmp/react-search-ui-demo/start-backend.py

# 터미널 2
cd frontend
PATH=/home/sh-test/.local/node/bin:$PATH npm run dev -- --host localhost
```

브라우저 URL: **http://localhost:5173/search**. 현재 두 개발 서버는 실행 중이다.
이미 실행 중인 서버가 있다면 중복 실행하지 않는다.
`/tmp` 데모는 이 작업 환경 전용이다. 다른 환경은 위 일반 실행 절차와 실제 DB를 사용한다.

## Architecture Notes

1. **연도는 필터만 지원한다.** 실제 Search / Document / Revision 응답에 `year` 또는
   `document_year` 필드가 없다. 따라서 카드·상세에 문서 연도를 표시하지 않는다.
   수정일이나 제목에서 연도를 추정하지 않았으며 존재하지 않는 응답 타입도 추가하지 않았다.
   Backend 계약 변경 없이 구현하라는 요청에 따라 보고만 한다.
2. **최신 버전의 처리 상태는 검색 응답에서 알 수 없다.** `has_newer_revision`은
   실패·대기 등 원인을 구분하지 못하므로 “새 버전 미반영”으로 표시한다.
   상세 이력의 `parse_status`는 파싱 상태이며 임베딩 성공/실패를 의미하지 않는다.
3. **SSO 미구현** 때문에 production에서 실제 직원 인증은 아직 불가능하다.
   개발 identity가 production 기능으로 간주되지 않도록 빌드에서 제거한다.

## Consistency Check / React Search UI / Next Step

**Consistency Check: PASS. React Search UI: READY (현재 API 계약 범위).**
요청된 문서 연도 표시의 API 제약과 production SSO 제약은 위에 명시했다.
다음 단계: **RAG Backend + Chat API 구현**.
