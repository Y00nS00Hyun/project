# DESIGN FREEZE v1

작성일: 2026-09-08 · **Design Freeze: READY** · **Consistency Check: PASS**

구현 기준은 [기능명세 v2.4](functional-spec-v2.4.md)와
[DB 스키마 v2.4](database-schema-v2.4.md)다. v2.3 및 기존 PoC 보고서는 수정 없이 이력으로 보존한다.
근거는 [HWP/HWPX Parser](poc/hwp-poc-report.md), [Korean Search](poc/korean-search-poc-report.md),
[Chunking + Embedding](poc/chunking-embedding-poc-report.md) PoC다.

READY는 API Contract와 구현을 진행할 설계 기준이 정리됐다는 뜻이다.
실제 사내 corpus 검증과 production 운영 승인은 별도로 남아 있다.
새로운 일반 PoC를 선행 과제로 추가하지 않는다.

## Frozen Decisions

| 항목 | 구현 기준 | 근거 / 상세 |
| --- | --- | --- |
| Database | PostgreSQL + pgvector, 16개 테이블 유지 | DB v2.4 §3/§31/§36 |
| Backend / Frontend | FastAPI / React + TypeScript + Vite | 기능명세 §2 |
| Parser | 기존 HWP/HWPX custom parser 재사용 | 기능명세 §6.5, DB §32.1 |
| Parse 결과 | `parse_status`는 실행 상태, `parse_result_code`는 결과 의미 | 기능명세 §8.2, DB §7/§12 |
| READY | parse SUCCESS + TEXT_EXTRACTED + embedding SUCCESS | summary/tagging 성공은 non-blocking, downstream SKIPPED 유지 |
| Chunking | Paragraph-aware / target·hard max **64 tokens provisional** / overlap **0** | 문단 경계 우선, 단일 초과 문단만 내부 분할, source anchor·chunking_version 유지 |
| Table | independent table-aware **OFF** | row/column/cell/span은 `parsed_structure`에 보존, `chunks.content_type` 추가 없음 |
| Embedding | local `intfloat/multilingual-e5-small` / **384d** | `query: ` / `passage: `, L2 normalization, provenance 기록 |
| Input budget | 모델 tokenizer 기준 chunk 64, prefix·special 포함 입력 최대 512 | 암묵적인 tokenizer truncation 금지 |
| Lexical | **pg_trgm** 별도 lexical search | simple FTS는 primary ranking engine으로 사용하지 않음 |
| Vector | **pgvector exact cosine** 초기 기본 | HNSW 미도입 |
| Aggregation | 전체 eligible chunk → document별 **MAX(score)** → document ranking → LIMIT | chunk top-K 후 dedup 금지 |
| Fusion | 기존 RRF 구현과 option 유지, **Automatic RRF = OFF** | 활성화 시 문서 단위 순위 간 결합 |
| Document version | **current READY revision**만 일반 검색·신규 RAG에 사용 | latest/current 분리 및 document 소속 복합 FK 보존 |
| ACL | 서버 identity 기반, **retrieval 이전 적용** | 모든 lexical/vector/RAG 경로에 동일 적용, MVP allow-only 유지 |
| Citation | HWP/HWPX paragraph anchor; DOCX paragraph 우선; PDF 검증된 page 허용 | 추정 page 금지, section_title optional·heuristic 금지 |
| No-answer | cosine threshold 하나만으로 근거 존재/refusal 판정 금지 | 숫자 threshold는 Freeze하지 않음 |

Embedding 초기 model revision은 `614241f622f53c4eeff9890bdc4f31cfecc418b3`이다.
실제 `embedding_provider/model/dimension/version`, `embedded_at`을 기록하며,
모델 변경으로 dimension이 달라지면 기존 chunks 재임베딩과 migration이 필요하다.

**64 tokens는 짧은 synthetic corpus에서 얻은 provisional default이며 실제 사내 장문
문서의 최적값으로 검증되지 않았다. IMPLEMENTATION DEFAULT ≠ FINAL PRODUCTION OPTIMUM.**
모델·청킹·lexical·fusion은 현재 구현 기본값으로 고정하되 actual corpus의 검증된 근거로 변경할 수 있다.
PoC의 지표 우승 C4(Fixed + e5-base)와 운영·근거 보존까지 고려한 기본 후보 C2를 구분한다.
C2에 대한 RRF 효과를 직접 검증한 것으로 주장하지 않는다.

DB v2.3→v2.4의 정의 변경은 `VECTOR(384)`, pg_trgm 확장 필수화,
선택적 FTS index의 기본 생성 제외다. 기존 FK/UNIQUE/CHECK 및 `is_ready` 생성식,
current/latest pointer, chunk·chat source FK, 태그 귀속, `processing_jobs.result_code`는 보존했다.
이번 작업은 문서 3개 생성으로 한정했으며 migration·서비스 코드·dataset을 변경하거나 commit하지 않았다.

## Open Decisions

| 항목 | 결정에 필요한 근거 / 시점 |
| --- | --- |
| 실제 사내 corpus에서 search 성능 | 대표 문서·질의·정답으로 측정 |
| 실제 장문 문서용 최적 chunk size | 64 provisional의 fragmentation/dilution 재검증 |
| 실제 긴 표 / 병합 셀 처리 | parser 구조 보존과 retrieval 근거 완전성 검증 |
| HNSW 적용 시점 | 실제 chunk 규모·검색 latency·RAM·Recall |
| RAG retrieval Top-K | RAG 구현·평가 |
| RAG context assembly | 인접 문단·표 header·값의 근거 연결 검증 |
| RAG refusal threshold | no-answer 포함 RAG 검증; cosine 단독 기준 금지 |
| PGroonga 최종 도입 여부 / RRF 활성화 | actual corpus에서 현재 기본값 대비 효과 |
| ACL DENY precedence / override | 보안 요구 및 USER/DEPARTMENT 충돌 규칙 확정 |
| worker production memory / concurrency | 실제 문서 구성·동시 처리 부하·최대 파일 크기 |

기존 retry backoff, File Sync missing grace period, Object Storage 이전 시점,
독립 normalizer 버전 관리도 운영 결정으로 남긴다. 외부 LLM/API·개발 도구의 사용 승인은
해당 사용 전에 확인한다. 로컬 embedding 구현을 외부 Embedding API 승인에 종속시키지 않는다.
OPEN 항목이 있다는 이유로 핵심 architecture 전체를 unfrozen으로 취급하지 않는다.

## Validation Gates

**현재 상태: 실제 사내 corpus 미확보·미검증.** 데이터 확보 후 실제 사내 운영 승인 전에 수행한다.

| Gate | 확인할 사항 |
| --- | --- |
| HWP/HWPX 재측정 | 사내 호환성, 결과 코드, paragraph anchor, 표·span, 암호화/배포용 비율, 자원 사용 |
| Search 재평가 | 전체/category 지표, no-answer, current READY 격리, retrieval 이전 ACL, leakage 0 |
| Chunking/Embedding 재평가 | 장문·긴 표의 fragmentation/dilution, 품질·latency·memory 및 dimension 비용 |

기존 harness·지표·provenance를 재사용하고 실제 corpus 결과를 기록한다.
이 gate는 **production validation**이며 새로운 일반 PoC를 반복하라는 의미가 아니다.
데이터 대기 중에도 API Contract·DB Migration·서비스 구현을 진행할 수 있으나
미검증 gate를 통과했다고 간주하지 않는다.

DOCX/PDF는 ingestion 구현의 **parser integration test**에서 추출과 citation mapping을 확인한다.
별도 대형 PoC나 검증되지 않은 page number를 만들지 않는다.

## Change Policy

Frozen decision은 실제 측정 결과, 보안 정책, 운영 제약 또는 명확한 blocker를 근거로 변경한다.
구현 편의나 근거 없는 선호만으로 핵심 architecture를 임의 변경하지 않는다.

변경 시 근거·대안·품질/보안/운영 영향과 API·DB·재임베딩·출처 보존에 미치는 범위를 기록하고,
기능명세·DB·Freeze 문서의 새 버전에 일관되게 반영한다. 기존 버전은 이력으로 유지한다.
OPEN 항목의 결정도 Frozen 기준에 영향을 주면 같은 변경 정책을 따른다.

다음 단계는 **API Contract v1 작성**이다.
