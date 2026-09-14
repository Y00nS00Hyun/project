# 사내 문서 관리 시스템

사내 공유폴더의 HWP·HWPX·DOCX·PDF 문서를 수집해 권한을 적용한 검색과 문서 기반 질의응답을
제공한다. 단일 Ubuntu VM + Docker Compose로 운영한다.

```text
공유폴더 (read-only)  ──►  ingestion  ──►  PostgreSQL + pgvector
                                                  │
                               ACL 적용 ──────────┤
                                                  ▼
                            React UI  ◄──  FastAPI  ──►  (선택) 외부 LLM
```

---

## 1. 시스템 개요

**공유폴더가 Source of Truth다.** 시스템은 공유폴더를 읽기만 한다. 원본 파일을
수정·삭제·이동·rename하지 않으며, 컨테이너에도 read-only로 마운트된다. DB는
공유폴더의 파생물이고, 둘이 어긋나면 공유폴더가 옳다.

| 구성 | 내용 |
|---|---|
| Backend | FastAPI (Python 3.10) |
| Frontend | React + Vite, nginx가 정적 서빙 및 `/api` 프록시 |
| DB | PostgreSQL 16 + pgvector, pg_trgm, pgcrypto |
| 임베딩 | `intfloat/multilingual-e5-small` (384차원, CPU, 오프라인 캐시) |
| 배포 | 단일 VM + Docker Compose |

**ACL은 검색보다 먼저 적용된다.** 후보 집합을 만드는 단일 지점(`ELIGIBLE_CTE`)에서
권한을 먼저 거르고, 그 결과 위에서만 랭킹과 RAG가 동작한다. 권한 없는 문서는
점수가 높아서 밀려나는 것이 아니라 애초에 후보에 들어오지 않는다.

**검색에는 current READY revision만 사용한다.** 파싱과 임베딩이 모두 성공한
revision만 READY이고, 그중 문서가 가리키는 current revision 하나만 검색·인용의
대상이 된다.

---

## 2. 지원 파일과 수집

### 형식

| 확장자 | 스캔 | 본문 추출 | 결과 |
|---|---|---|---|
| `.hwpx` | O | O | 검색 가능 |
| `.hwp` | O | O | 검색 가능 |
| `.docx` | O | O | 검색 가능 |
| `.pdf` | O | O (text layer가 있는 PDF) | 검색 가능. 이미지-only PDF는 `OCR_REQUIRED`, 검색 불가 |

파서는 HWPX·HWP·DOCX·PDF 넷이 등록되어 있다(`src/document_processing/parsers/__init__.py`).
형식 추가는 파서 registry에 등록하는 작업이다.

**DOCX**는 paragraph와 표 셀 텍스트를 추출하고, **문서에 선언된 순서를 그대로 지킨다** —
문단·표·문단으로 쓰인 문서는 추출 결과도 그 순서다. 표 안의 텍스트도 검색된다. 원본
레이아웃 재현이 목적이 아니므로 이미지 OCR, 도형·SmartArt, 머리글/바닥글, 변경 내용
추적은 지원하지 않는다. `.docm`은 대상이 아니다.

**PDF**는 pypdf로 **text layer에 있는 텍스트만** 추출한다. 여러 페이지를 지원하고, 각 블록에
파일 안의 실제 페이지 번호를 붙여 검색 결과·원문 미리보기의 page anchor(`N페이지`)로 쓴다.
chunk는 page 경계를 넘어 합치지 않는다. 텍스트 기반 PDF는 다른 형식과 똑같이
parse → chunk → embedding → READY를 거쳐 검색된다. 표의 행/열 구조나 원본 layout은 복원하지
않으며, text layer에 들어 있는 표 글자는 일반 텍스트로 검색된다.

| PDF 상태 | 결과 |
|---|---|
| 이미지만 있는 PDF (스캔본) | `OCR_REQUIRED` — OCR하지 않으며 검색 대상이 아니다 |
| 텍스트도 이미지도 없는 PDF | `EMPTY_DOCUMENT` |
| 손상된 PDF | `CORRUPT` |
| 비밀번호가 필요한 PDF | `ENCRYPTED` |
| AES 암호화 PDF | crypto 의존성이 없어, 인쇄 제한만 걸린 경우에도 `ENCRYPTED`(detail `CRYPTO_LIBRARY_REQUIRED`)로 처리될 수 있다 |

OCR, PDF viewer, thumbnail, layout 복원은 지원하지 않는다.

파서가 생기기 전에 수집되어 `UNSUPPORTED_FORMAT`으로 남은 DOCX·PDF는 파일이 그대로면 해시도
같아 일반 scan이 unchanged로 넘긴다. 다음 명령이 그 revision을 다시 parse 큐에 넣는다 —
새 document나 revision을 만들지 않고 기존 revision을 재사용한다.

```bash
docker compose exec backend python -m ingestion reparse-unsupported
docker compose exec backend python -m ingestion run
```

### 자동 수집

systemd timer가 60초마다 `ingestion run`(스캔 → 파싱 → 임베딩)을 실행한다.
중복 실행은 `flock`으로 막는다. 설치와 상태 확인은 `docs/deployment.md` §4.1.

### 파일 변화의 해석

| 공유폴더에서 일어난 일 | 시스템의 처리 |
|---|---|
| 새 파일 등장 | 새 document 생성 |
| 같은 경로에서 내용 수정 | 같은 document에 **새 revision** 추가 |
| 내용이 같은 rename / 폴더 이동 | **같은 document 유지** (id·권한·인용 보존) |
| rename과 내용 수정이 동시에 | 내용 해시가 달라지므로 **새 document** |
| 파일이 사라짐 | missing 표시 후 유예기간 경과 시 soft delete (hard delete 없음) |

rename/move 판정은 내용 해시가 같고 사라진 쪽과 나타난 쪽이 **각각 하나씩일 때만**
성립한다. 이름의 유사성은 보지 않는다.

**이전 scan부터 이미 missing이던 문서는 rename 후보가 아니다.** 같은 해시의 파일이
나중에 등장해도 자동으로 연결하지 않고 새 document로 만든다. 사라진 시점과 나타난
시점 사이에 여러 scan이 지났다면 백업이나 사본 복원일 가능성이 높고, 잘못 연결하면
과거 문서의 이력과 인용이 무관한 파일에 붙기 때문이다. 대상은
`missing_since IS NULL`인 문서, 즉 **직전 scan에는 존재했던** 문서로 한정된다.

---

## 3. 문서 상태

문서는 revision 포인터를 둘 가진다.

- **`current_revision_id`** — 지금 검색·인용에 쓰이는 revision. READY만 올 수 있다.
- **`latest_revision_id`** — 공유폴더에서 마지막으로 감지한 revision. 아직 처리
  중일 수 있다.

둘이 다르면 **최신 파일을 처리하는 중이고, 그동안 검색은 이전 READY revision을
사용한다.** 이 상태는 문서 상세 화면에 안내된다. 처리가 끝나면 두 값이 같아진다.

READY는 파싱 성공 + 본문 추출 + 임베딩 성공을 모두 만족한 상태다(생성 컬럼
`is_ready`). 요약 생성 여부는 READY 조건에 포함되지 않는다.

---

## 4. 검색

의미 기반 검색(pgvector 코사인 유사도)이 기본이고, 두 가지 보정을 얹는다.

- **제목 가중치** — 질의어가 제목과 겹치면 가산 (`TITLE_BOOST_WEIGHT`, 기본 0.075)
- **선택적 본문 완전 일치 가중치** — 질의 문자열이 본문에 그대로 등장하는 문서에
  가산 (`BODY_EXACT_BOOST_WEIGHT`, 기본 0.10). 단, 그 문자열이 후보 집합의 일정
  비율을 넘게 등장하면 변별력이 없다고 보고 **발동하지 않는다**
  (`BODY_EXACT_SELECTIVITY_MAX`, 기본 0.50).

> `BODY_EXACT_SELECTIVITY_MAX = 0.50`은 **provisional 값이다.** 7건 규모의
> corpus에서 측정한 것이라 실제 운영 규모에서는 재측정이 필요하다.
> 선택도는 ACL을 통과한 current READY 문서 집합을 분모로 계산한다.

필터는 폴더 경로, 연도, 태그, 파일 형식, 부서를 지원한다. 부서 필터는 API에는
남아 있으나 회사가 부서 구분을 쓰지 않아 UI에는 노출하지 않는다.

모든 필터는 ACL 적용 **후**에 동작한다. 필터를 조작해 권한 밖 문서를 끌어낼 수
없다. 점수·유사도 같은 내부 수치는 API와 UI 어디에도 노출하지 않는다.

---

## 5. 인증과 권한

회사에 SSO/OIDC/SAML/LDAP/AD가 없어 **Local Auth가 실제 운영 인증 방식이다.**

```text
/signup 가입  ──►  PENDING  ──►  관리자 승인  ──►  ACTIVE  ──►  로그인 가능
```

- 비밀번호는 Argon2id 해시로만 저장한다. 관리자도 원문을 볼 수 없다.
- 세션은 서버에 저장하고 브라우저에는 **HttpOnly 쿠키**만 내려간다.
- 가입만으로 관리자 권한을 얻을 수 없고, 승인 전에는 어떤 API도 통과하지 못한다.
- 계정 비활성화 시 해당 사용자의 세션은 즉시 모두 무효화된다.

### 문서 접근 정책

승인된 ACTIVE 사용자는 등록된 모든 문서를 읽을 수 있다. 이는 ACL을 우회하는 것이
아니라 `document_permissions`의 **세 번째 principal(`is_public`)** 로 표현된다.
권한 행이 하나도 없는 문서는 여전히 아무도 볼 수 없다(default deny).

> **운영 전제:** 이 시스템에는 전 직원 열람이 가능한 일반 문서만 등록한다.
> 기밀·인사·급여·특정 사용자 전용 문서는 **수집 대상에서 제외한다.** 사용자별
> 문서 권한 관리 UI는 두지 않으며, 개별 권한이 필요하면 CLI로 부여한다.

---

## 6. 문서 상세 화면

- **원문 텍스트 미리보기** — 수집 과정에서 이미 추출해 둔 텍스트를 페이지 단위로
  보여준다. 원본 레이아웃 뷰어가 아니라 **추출된 텍스트**이며, 표나 단 구성은
  재현되지 않는다. 권한 확인 후 current READY revision의 본문만 반환하고, 긴 문서
  전체가 상세 응답에 실리지 않도록 별도 요청으로 분리되어 있다.
- **원본 다운로드** — 문서 id만으로 요청한다. 서버의 절대 경로는 API·UI 어디에도
  노출되지 않는다.
- **버전 이력** — revision이 2개 이상일 때만 접이식 섹션으로 표시한다. 1개면 상단
  정보와 중복이라 숨긴다. 표시 여부만 조건부이고 revision 기능 자체는 그대로다.
- **처리 중 안내** — current와 latest가 다르면 경고와 함께 양쪽 revision을 보여준다.

---

## 7. AI 기능 (요약 / 문서 질의응답)

코드는 구현되어 있으나 **현재 비활성 상태다.**

```bash
LLM_PROVIDER=                      # 비어 있음 → 요약·채팅 모두 동작하지 않음
DOCUMENT_EXTERNAL_LLM_ENABLED=false  # 기본값
```

provider가 없으면 UI는 "AI 질문 기능이 현재 비활성화되어 있습니다"를 표시하고
입력창을 비활성화한다. 요약은 생성되지 않고 `SKIPPED` 상태로 남는다.

`DOCUMENT_EXTERNAL_LLM_ENABLED`는 **`LLM_PROVIDER`와 별개인 안전장치다.** API 키가
있다는 사실만으로는 켜지지 않는다. 이 값이 false인 동안 실제 사내 문서 본문은
외부 LLM으로 전송되지 않는다. 합성·비민감 문서로 검증하는 환경에서만 명시적으로
true로 바꾼다.

설계상 지켜지는 제약: 검색된 본문은 신뢰할 수 없는 입력으로 다루고, 인용은
허용 목록 안에서만 가능하며, 근거 없는 답변은 생성하지 않고, 문서 범위 제한은
프롬프트가 아니라 검색 단계에서 강제한다.

---

## 8. 운영

전체 절차는 **[docs/deployment.md](docs/deployment.md)** 에 있다. 자주 쓰는 것만:

```bash
# 기동 / 상태 / 로그
docker compose up -d
docker compose ps
docker compose logs -f backend

# 수집 (평소에는 timer가 자동 실행한다)
docker compose exec backend python -m ingestion run

# 수집 timer
sudo deploy/systemd/install.sh
systemctl status docsearch-ingest.timer
journalctl -u docsearch-ingest.service -n 50

# 계정
docker compose exec backend python -m auth.cli list-users
docker compose exec backend python -m auth.cli approve --login-id <id>

# 백업 — 매일 03:00 자동 실행된다. 아래는 상태 확인과 수동 1회 실행
systemctl status docsearch-backup.timer
journalctl -u docsearch-backup.service --since today
sudo systemctl start docsearch-backup.service

# 복구 리허설 (운영 DB는 건드리지 않는다)
scripts/db-restore.sh backups/<dump>
```

**TLS는 실운영 전에 반드시 필요하다.** 현재 미구성이며, 그 상태에서는 세션 쿠키가
평문 HTTP로 오간다. HTTPS를 적용한 뒤에 `AUTH_COOKIE_SECURE=true`로 바꾼다. 그
전에 켜면 쿠키가 전송되지 않아 로그인이 되지 않는다.

---

## 9. 아직 하지 않은 것

- **실제 회사 공유폴더 연결** — 현재는 테스트용 폴더를 대상으로 동작한다.
- **대규모 corpus 검증** — 현재 12건 규모. 처리량, 검색 응답시간, 그리고 위의
  provisional 파라미터는 실제 규모에서 다시 측정해야 한다.
- **스캔 PDF OCR / 복잡한 layout 복원** — text layer가 없는 PDF는 검색되지 않는다.
- **외부 LLM 실제 활성화** — provider 설정과 safety gate 해제 모두 명시적 결정이
  필요하다.
- **백업의 VM 외부 보관** — 매일 03:00 로컬 자동 백업은 동작한다. 덤프가 원본과
  같은 VM에 있어 운영 실수·잘못된 migration·Docker volume 손실은 복구되지만,
  디스크나 VM 전체 손실에는 대응하지 못한다.
- TLS, 로그 수집, 메트릭/알림.

---

## 10. 문서 안내

현재 상태를 반영하는 문서는 다음 넷이다.

| 문서 | 내용 |
|---|---|
| 이 README | 시스템 개요와 현재 구현 상태 |
| [docs/deployment.md](docs/deployment.md) | 배포·운영 절차 |
| [docs/database-schema-v2.5.md](docs/database-schema-v2.5.md) | 현행 DB 스키마 |
| [docs/api-contract-v1.3.md](docs/api-contract-v1.3.md) | 현행 API 계약 |

`docs/` 의 나머지 문서는 **구현 과정의 기록이다.** 버전이 낮은 스키마·명세·계약
문서와 각 서브시스템 설계 문서(`search-backend.md`, `ingestion-foundation.md`,
`rag-chat-backend.md`, `embedding-pipeline.md`, `http-api-search-documents.md`)는
작성 시점의 상태를 담고 있어 현재 구현과 다를 수 있다. 현재 동작의 근거는 코드와
테스트이며, 위 네 문서가 그 다음이다.
