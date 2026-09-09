# Deployment — single VM, docker compose

현재 VM 한 대에서 전체 스택을 실행하기 위한 **운영 절차서**다.
설계 근거는 `docs/design-freeze-v1.md`와 `docs/api-contract-v1.md`에 있고,
이 문서는 실제로 입력할 명령만 다룬다.

```text
Browser
   │
   ▼
nginx  (호스트에 공개되는 유일한 포트)
   ├── /        →  React 정적 파일
   └── /api/    →  backend:8000  (FastAPI)
                      │
                      ▼
                 postgres:5432  (pgvector)

backend
 ├── 공유폴더        read-only 마운트
 ├── 임베딩 모델     로컬 캐시 read-only 마운트, 실행 중 다운로드 없음
 └── Claude API      명시적으로 켰을 때만
```

---

## 1. Prerequisites

```text
Ubuntu Linux VM (단일 노드)
Git
Docker Engine
Docker Compose v2 이상
```

검증에 사용한 버전:

```text
Docker Engine    29.8.0
Docker Compose   v5.5.1
PostgreSQL       16.15 (pgvector/pgvector:pg16)
```

`docker` 명령을 sudo 없이 실행할 수 있어야 한다.

---

## 2. First deployment

```bash
cd /home/sh-test/project

cp .env.example .env
nano .env          # 아래 4개는 반드시 채운다

docker compose config     # 렌더링 및 문법 확인
docker compose build
docker compose up -d
docker compose ps
```

`.env`에서 반드시 채워야 하는 값:

| 변수 | 설명 |
| --- | --- |
| `POSTGRES_PASSWORD` | 예: `openssl rand -base64 24`. 기본값 없음 |
| `DATABASE_URL` | 위 비밀번호와 일치시킨다. 호스트는 `postgres` |
| `SHARED_FOLDER_HOST_PATH` | 색인할 공유폴더의 호스트 절대경로 |
| `EMBEDDING_CACHE_HOST_PATH` | 사전 준비된 모델 캐시의 호스트 절대경로 (§7) |

이 네 개는 기본값이 없다. 비워 두면 `docker compose` 자체가 실패한다.
잘못된 디렉터리를 조용히 색인하는 것보다 기동하지 않는 편이 안전하기 때문이다.

정상 상태:

```text
NAME                  SERVICE    STATUS
docsearch-postgres-1  postgres   Up (healthy)
docsearch-migrate-1   migrate    Exited (0)
docsearch-backend-1   backend    Up (healthy)
docsearch-frontend-1  frontend   Up (healthy)
```

`migrate`가 `Exited (0)`인 것이 정상이다. 한 번 실행되고 끝나는 서비스다.

접속:

```text
http://<VM-IP>:<APP_HTTP_PORT>/search
http://<VM-IP>:<APP_HTTP_PORT>/chat
```

---

## 3. Logs

```bash
docker compose logs backend
docker compose logs frontend
docker compose logs postgres
docker compose logs migrate

docker compose logs -f backend      # 실시간
```

로그는 서비스당 10MB × 3개로 회전한다(`compose.yaml`의 `x-logging`).
단일 VM에 로그 수집기가 없으므로 상한이 없으면 디스크가 찬다.

검색어 전문, 문서 본문, 전체 RAG 컨텍스트, Claude 응답 전문, API 키는
로그에 남지 않는다. 이는 애플리케이션 정책이며 배포가 바꾸지 않는다.

---

## 4. Ingestion

색인은 **API 기동과 분리되어 있다.** backend를 재시작해도 공유폴더를
다시 훑지 않는다. 운영자가 명시적으로 실행한다.

```bash
# 전체 파이프라인: 파일 발견 → 파싱 → 임베딩
docker compose exec backend python -m ingestion run

# 단계별 실행
docker compose exec backend python -m ingestion sync     # 파일 발견 / revision 생성
docker compose exec backend python -m ingestion parse    # PARSE 작업 처리
docker compose exec backend python -m ingestion embed    # EMBED 작업 처리

# 한 번에 처리할 작업 수 조정 (기본 100)
docker compose exec backend python -m ingestion run --limit 500
```

출력은 건수뿐이다. 문서 경로와 본문은 출력되지 않는다.

`--root`를 주지 않으면 컨테이너의 `SHARED_ROOT`(기본 `/data/shared`)를 쓴다.

---

## 5. Shutdown

```bash
docker compose down
```

`postgres_data`는 named volume이라 위 명령으로 사라지지 않는다.

```bash
docker compose down -v      # ← 데이터베이스를 삭제한다. 일상 운영에서 쓰지 않는다
```

---

## 6. Update

```bash
cd /home/sh-test/project

git pull
docker compose build
docker compose up -d
docker compose ps
```

`docker compose up -d`는 `migrate`를 다시 실행한다.
새 migration이 없으면 alembic이 아무 것도 하지 않고 즉시 종료하므로
반복 실행해도 안전하다.

마이그레이션이 실패하면 `migrate`가 0이 아닌 코드로 종료하고,
`backend`는 `service_completed_successfully` 조건 때문에 **기동하지 않는다.**
스키마가 반쯤 적용된 상태로 API가 뜨는 일은 없다.

실패 시:

```bash
docker compose logs migrate
```

---

## 7. Model cache

임베딩 모델은 **실행 중에 다운로드하지 않는다.** 컨테이너는
`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`로 동작하므로 캐시가 없으면
조용히 500MB를 받아오는 대신 큰 소리로 실패한다.

필요한 것:

```text
모델      intfloat/multilingual-e5-small
revision  614241f622f53c4eeff9890bdc4f31cfecc418b3
차원      384  (chunks.embedding VECTOR(384)와 일치해야 한다)
```

`EMBEDDING_CACHE_HOST_PATH`가 가리키는 디렉터리는 Hugging Face hub 캐시
레이아웃이어야 한다.

```text
<EMBEDDING_CACHE_HOST_PATH>/
└── models--intfloat--multilingual-e5-small/
    └── snapshots/614241f622f53c4eeff9890bdc4f31cfecc418b3/
```

확인:

```bash
ls "$EMBEDDING_CACHE_HOST_PATH"/models--intfloat--multilingual-e5-small/snapshots/
```

캐시가 없는 새 VM에서는 **인터넷이 되는 장비에서 미리 받아 복사한다.**
기동 경로에 자동 다운로드를 넣지 않는다.

```bash
# 인터넷이 되는 장비에서 한 번만
python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download(
    "intfloat/multilingual-e5-small",
    revision="614241f622f53c4eeff9890bdc4f31cfecc418b3",
    cache_dir="./model-cache",
)
PY

# 그 다음 ./model-cache 를 VM으로 복사하고 EMBEDDING_CACHE_HOST_PATH 로 지정
```

마운트는 read-only다. 컨테이너가 캐시를 갱신하지 않는다.

**캐시 파일은 컨테이너 사용자가 읽을 수 있어야 한다.** backend는 uid 10001로
실행되므로, 호스트에서 `0600`으로 만들어진 파일은 읽지 못한다. 모델 로딩 자체는
성공하지만 huggingface_hub이 선택적 메타데이터를 읽지 못해 다음과 같은 오해를
부르는 경고를 남긴다.

```text
Ignoring corrupted tree cache file ...: [Errno 13] Permission denied
```

파일이 손상된 것이 아니라 권한 문제다. 정리하려면:

```bash
chmod -R a+rX "$EMBEDDING_CACHE_HOST_PATH"
```

---

## 8. Shared folder

```text
호스트 경로     SHARED_FOLDER_HOST_PATH   (.env, 기본값 없음)
컨테이너 경로   /data/shared              (SHARED_ROOT)
마운트          read-only
```

시스템은 공유폴더의 **읽기 전용 소비자**다. 원본을 수정·삭제·이름변경·이동하지
않으며, `:ro` 마운트가 그것을 코드가 아니라 커널 수준에서 보장한다.

`/`, `$HOME`, repository 자신을 기본 마운트하지 않는다. 경로는 항상 명시한다.

확인:

```bash
docker compose config | grep -A2 '/data/shared'      # read_only: true 여야 한다
```

---

## 9. Claude enable

**기본값은 꺼져 있다.** `LLM_PROVIDER`가 비어 있으면 provider는
`UnconfiguredProvider`이고 외부로 나가는 요청이 없다.
환경에 API 키가 남아 있다는 것만으로는 켜지지 않는다 — provider를 명시해야 한다.

켜려면 `.env`에서:

```text
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=<발급받은 키>
```

그리고:

```bash
docker compose up -d backend
```

선택 항목: `ANTHROPIC_MODEL`, `ANTHROPIC_MAX_TOKENS`,
`ANTHROPIC_TIMEOUT_SECONDS`, `ANTHROPIC_MAX_RETRIES`, `ANTHROPIC_EFFORT`.
비워 두면 `src/rag/providers/anthropic_claude.py`의 기본값을 쓴다.

`ANTHROPIC_API_KEY`는 backend 컨테이너에만 전달된다. frontend 이미지,
`VITE_*` 변수, JS 번들 어디에도 들어가지 않는다.

> **사내 문서를 외부 서비스로 보내는 것은 조직의 승인이 선행되어야 한다.**
> 승인 전에는 켜지 않는다.

---

## 10. Authentication / SSO

```text
Production SSO    NOT IMPLEMENTED
```

`APP_ENV=production`에서 `require_user()`는 모든 요청을 401로 거절한다.
아직 인증 공급자가 없기 때문이며, 이것이 fail-closed 기본값이다.

따라서 **UI는 정상적으로 뜨지만 검색/채팅 API는 401을 반환한다.**
이는 배포 실패가 아니다.

```text
배포 인프라 동작   ≠   직원 인증 동작
```

개발용 `X-Debug-User-Id` 헤더는 `APP_ENV`가
`test/testing/development/dev/local`일 때만 동작하며,
production 프론트엔드 번들에는 그 코드 자체가 컴파일되어 남지 않는다.
배포 편의를 위해 이것을 production에서 켜지 않는다.

다음 단계는 조직의 실제 인증 방식(SAML / OIDC / LDAP / 사내 게이트웨이)을
확인한 뒤 진행한다.

---

## 11. Ports and firewall

```text
호스트에 공개    APP_HTTP_PORT  (기본 8080)  →  nginx
공개하지 않음    5432 (PostgreSQL), 8000 (FastAPI)
```

PostgreSQL과 FastAPI는 compose 내부 네트워크에만 존재한다.
외부에서 DB에 직접 붙거나 nginx를 우회해 API를 호출할 수 없다.

방화벽/보안그룹은 이 절차에서 변경하지 않았다.
외부 PC에서 접근하려면 `APP_HTTP_PORT`를 여는 것은 **운영자가 직접** 수행한다.

---

## 12. TLS

이 단계에서는 HTTPS를 구성하지 않았다. 현재는 평문 HTTP다.

```text
http://<VM-IP>:<APP_HTTP_PORT>/
```

**실운영 전 HTTPS가 필요하다.** 사내망이라도 인증 쿠키/세션이 도입되는
순간 평문 전송은 받아들일 수 없다. 리버스 프록시 앞단에 TLS 종단
(사내 인증서 또는 사내 CA)을 두는 것이 다음 배포 과제다.

---

## 13. Known limitations

```text
TLS/HTTPS          미구성. 실운영 전 필수
SSO                미구현. 현재 production API는 전부 401
DB backup          미구현 ← 아래
로그 수집          없음. docker compose logs 로만 확인
메트릭/알림        없음
```

### DB backup

백업 전략은 아직 구현하지 않았다. 현재는 `postgres_data` named volume에만
데이터가 있고, VM 디스크가 손상되면 색인 결과 전체를 잃는다.
원본 문서는 공유폴더에 남아 있으므로 재색인은 가능하지만,
파싱 + 임베딩을 처음부터 다시 돌려야 한다.

**실운영 전에 정해야 할 것:**

```text
pg_dump 주기와 보관 위치
volume 스냅샷 여부
복구 절차의 실제 리허설
```

수동 백업이 급히 필요하면:

```bash
docker compose exec postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > backup.sql
```

이 명령은 검증되었지만 **자동화된 백업 정책은 아니다.**

---

## 14. Troubleshooting

**`docker compose up`이 변수 오류로 실패한다**
`.env`의 `POSTGRES_PASSWORD`, `DATABASE_URL`, `SHARED_FOLDER_HOST_PATH`,
`EMBEDDING_CACHE_HOST_PATH`가 채워져 있는지 확인한다. 기본값이 없다.

**backend가 기동하지 않는다**
`docker compose logs migrate`를 먼저 본다. 마이그레이션이 실패하면
backend는 의도적으로 뜨지 않는다.

**임베딩이 실패한다**
`docker compose logs backend`에서 모델 캐시 오류를 확인하고 §7의 경로
구조를 점검한다. 자동 다운로드는 없다.

**UI는 뜨는데 검색이 401이다**
정상이다. §10을 참고한다. SSO 미구현.

**nginx 502**
backend가 아직 healthy가 아니거나 죽어 있다. `docker compose ps` 확인.
