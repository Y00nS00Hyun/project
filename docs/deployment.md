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
DB backup          스크립트/절차 검증 완료. 자동 스케줄과 원격 보관은 미구현 ← 아래
로그 수집          없음. docker compose logs 로만 확인
메트릭/알림        없음
```

### DB backup

백업/복구 **절차와 스크립트는 구현되어 있고 실제 복구까지 검증했다.**
아직 없는 것은 **자동 실행 스케줄과 원격 보관**이다.

```bash
scripts/db-backup.sh                 # backups/docsearch-<timestamp>.dump
scripts/db-backup.sh /mnt/nas/backup # 다른 위치에 저장
```

`pg_dump -Fc`(custom format)로 덤프한 뒤 `pg_restore --list`로 아카이브를
읽어 검증한다. 잘린 덤프는 이 시점에 걸러져 `.suspect`로 이름이 바뀐다.
`BACKUP_KEEP`(기본 7)개를 넘는 오래된 덤프는 자동 삭제된다.

복구는 **기본이 리허설**이다.

```bash
scripts/db-restore.sh backups/docsearch-20260909-074715.dump
```

별도 데이터베이스 `docsearch_restore_test`에 복원하고 행 수를 보고한다.
운영 DB는 건드리지 않는다. 끝나면 안내되는 `DROP DATABASE`로 정리한다.

운영 DB를 실제로 덮어쓰는 재해 복구는 명시적으로 요청해야 한다.

```bash
scripts/db-restore.sh <dump> --into-production
```

데이터베이스 이름을 직접 입력해야 진행되고, backend를 먼저 정지시켜
연결을 끊은 뒤 복원하고 다시 기동한다.

**검증 결과(2026-09-09):** 합성 문서 12건 / chunk 12건 / 384차원 벡터 12건 /
사용자 4명 / 권한 12건을 백업 → 복원한 뒤 원본과 지문(md5)을 대조해
documents, revisions, chunks, embeddings, search_vector, users, permissions
**7종 전부 일치**를 확인했다. 복원본에서 pgvector 연산과
`current_ready_chunks` 뷰가 정상 동작했다.

**아직 없는 것:**

```text
자동 실행 스케줄 (cron / systemd timer)
VM 외부 원격 보관    ← 디스크가 통째로 죽으면 backups/ 도 같이 죽는다
보관 주기 정책
```

복구 경로는 둘이다. 덤프에서 복원하거나, **공유폴더에서 재색인**하는 것이다.
원본은 공유폴더에 남아 있으므로 후자도 가능하지만 파싱 + 임베딩을 처음부터
다시 돌려야 한다. 백업 주기는 "재색인에 걸리는 시간"과 견줘서 정하면 된다.

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

backend가 healthy인데도 502가 계속 나온다면 nginx가 옛 컨테이너 IP를 붙잡고
있는 경우다. `frontend/nginx.conf`는 Docker 내장 DNS를 요청마다 다시 조회하도록
구성되어 있어 정상적으로는 발생하지 않지만, 확인은 이렇게 한다.

```bash
docker inspect docsearch-backend-1 \
  --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
docker compose logs frontend --tail 20 | grep -oE '[0-9.]+:8000'
```

두 IP가 다르면 `docker compose restart frontend`로 즉시 해소된다.

**한글 검색이 항상 0건이다 (오류는 없음)**
`pg_trgm`이 한글에서 trigram을 만들지 못하는 상태다. 데이터베이스 ctype이
`C`이면 발생한다.

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -tAc "SELECT datctype FROM pg_database WHERE datname = current_database();"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -tAc "SELECT show_trgm('예산');"
```

`datctype`이 `C.utf8`이어야 하고 `show_trgm`이 비어 있으면 안 된다.
`C`로 나오면 백업 후 데이터베이스를 `C.utf8`로 재생성하고 복원해야 한다
(§13 DB backup 참고). semantic 검색은 이 문제와 무관하게 동작하므로
증상이 "일부 검색만 안 됨"으로 보인다.
