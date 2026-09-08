# Chunking + Embedding PoC 보고서

작성일: 2026-09-08. **Synthetic corpus / CPU local inference**.
Authoritative specification: [기능명세 v2.3](../functional-spec-v2.3.md),
[DB v2.3](../database-schema-v2.3.md). 두 문서의 본문과 버전을 확인했으며 수정하지 않았다.
선행 결과: [HWP/HWPX Parser](hwp-poc-report.md), [한국어 Search](korean-search-poc-report.md).
코드·재현 명령: [실행 안내](../../poc/search/README-chunking-embedding.md).
측정 원본: [artifacts](../../artifacts/chunking-embedding-poc/).

## 1. Executive Summary

**PASS WITH LIMITATIONS**. 원본 문서 20건·질의 30건을 유지하면서 3개 chunker,
2개 로컬 모델, 2개 크기로 **9개 vector configuration**을 실제 평가했다.
모든 질의를 유지했고, 우승 configuration 한 개에만 pg_trgm + RRF를 재검증했다.

**QUALITY + OPERABILITY 기준의 PROVISIONAL PRODUCTION CANDIDATE는 C2:
Paragraph / 64 tokens / overlap 0 / e5-small / 384d / table-aware off**다.
R@1 0.8661, R@5 0.9911, MRR 0.9732, nDCG@5 0.9736, primary-at-1 0.9643이다.
문단·금액 경계를 보존하면서 작은 모델의 CPU 비용을 유지한다.

검색 지표만으로 선정한 우승은 **C4: Fixed 64 + e5-base**다. MRR 1.0000,
nDCG@5 0.9871로 더 높지만 C2와 R@5·primary-at-1이 같고, query P95는
39.0 → 109.8 ms, corpus embedding은 1.68 → 6.49초다. Fixed의 숫자·문장
분할 사례도 확인했다. §13에서 이 차이를 반영해 구현 기본값을 선정했다.

**실제 사내 corpus는 여전히 미검증이다.** 구조 주석도 native HWP 표가 아닌 synthetic이다.
짧은 문서에 맞춘 64토큰은 회사 문서의 최적값이 아니다. 결과는 구현 시작에 필요한
잠정 기본값을 제공하며, RAG 답변 품질이나 실제 사내 recall을 입증하지 않는다.

## 2. Scope

Fixed / Paragraph / Paragraph+Table 비교, e5-small / e5-base 비교, exact vector retrieval,
기존 지표·12 category, 청크 통계, 표·fragmentation·dilution, CPU 시간·메모리,
best vector의 RRF 재검증을 수행했다.

기존 `evaluate.py`의 지표와 문서 단위 집계, `VectorSearch`, `TrigramSearch`,
`HybridSearch`, PoC DB 로더를 재사용했다. DB 로더에 dimension과 임시 서버 종료 옵션을
추가했으며 기존 기본 dimension 384와 기존 Search 동작은 유지했다.
임시 PostgreSQL 16.2, pgvector 0.6.2, pg_trgm 1.6에서 측정했다.

FTS/PGroonga 재연구, HNSW·reranker·overlap tuning, backend/UI/SSO/ACL/Sync/worker,
RAG Answer Generation, Claude/API embedding, 자동 요약·태깅·업무 보고서 생성,
MCP, production migration은 수행하지 않았다. 자동 commit도 하지 않았다.

## 3. Dataset

기존 `poc/search/fixtures/documents.jsonl` 20건,
`queries.jsonl` 30건(정답 있음 28, no_answer 2)을 **파일 그대로 재사용**했다.
문서 추가·확장, query 변경, 정답·primary·category 변경은 없다.
실행 전후 입력 및 v2.3 두 문서의 SHA256 일치를 검사했다.
해시는 [configurations.json](../../artifacts/chunking-embedding-poc/configurations.json)에 있다.

원본에는 paragraph/table 배열이 없다. 다음 구조 주석을 모든 설정에 공통 적용했다.

- 기존 sentence baseline과 같은 `다. ` 경계를 **가상 문단**으로 표현했다.
- DOC-003의 예산 세부내역, DOC-006/007의 출장 일비·숙박비·식비를 3개 표로 표현했다.
- 출처 substring과 표 cells는 [chunking_layout.json](../../poc/search/fixtures/chunking_layout.json)에
  명시했다. 금액·연도·상한·실비 정산·일 단위 조건을 유지했다. 추가된 표 헤더는
  `세부 내역 | 금액`, `출장비 항목 | 지급기준`이라는 구조용 레이블이다.
- 모든 청커가 **같은 표 직렬화 텍스트**를 받는다. C만 유리한 사실을 받지 않는다.
  lexical은 이전의 원본 searchable content를 그대로 사용한다.
- 제목·부서·연도는 첫 블록에 한 번 들어간다. 이전 Search의 제목 독립 chunk 1개 +
  문장별 chunk 방식(108 chunks)과는 다르다.

원본 메타데이터 포함 길이는 e5-small 기준 **58~90 tokens**, 실험용 구조 표현은
**59~94 tokens**다. 256/512는 모두 한 덩어리가 되어 크기 효과를 측정할 수 없으므로
**64/128 두 크기만** 사용했다. 문서 길이 실측은 `model_measurements.json`에 있다.
임의의 장문을 붙여 검색 난도를 바꾸지 않았다.

이 corpus는 **실제 사내 문서를 대표하지 않는다**. 표 구조 및 paragraph anchor도
parser의 실제 위치를 뜻하지 않는다. 기존 평가 지표와 라벨은 비교 가능하지만,
선행 Search 수치와의 차이를 embedding 단독의 인과 효과로 해석할 수는 없다.

## 4. Chunking Strategies

| 방식 | 정책 | 경계·위치 처리 |
|---|---|---|
| A Fixed | token 상한까지 연속 분할 | 문단/행/금액 중간도 분할. Unicode 원문 offset 보존 |
| B Paragraph | 문단을 상한 내 누적 | 단일 문단이 상한보다 길 때만 내부 분할 |
| C Paragraph+Table | 본문은 B, 표는 독립 | 긴 표는 header 반복 + row group, 본문과 분리 |

Target와 hard maximum은 각각 64 또는 128로 동일하며 **overlap=0**이다.
토큰 수는 prefix/special tokens를 제외한 tokenizer 길이다. 실제 embedding 직전에
prefix/special tokens까지 포함해 512 제한을 검사하며 implicit truncation을 금지한다.
헤더+한 행이 상한을 넘는 표는 명시적으로 실패한다. 이번 corpus에서는 발생하지 않았다.

모든 chunk는 `document_id`, `chunk_index`, `text`, `token_count`, `content_type`,
`paragraph_start`, `paragraph_end`를 유지한다. 범위는 inclusive 0-based synthetic block
index다. `content_type=TABLE`은 C의 독립 표 chunk에만 붙으며 A/B에서는 TEXT다.
A/B의 table chunk 수 0은 표 내용이 없다는 뜻이 아니다.

두 모델의 tokenizer 설정 직렬화는 다르지만 C1/C4, C2/C5, C3/C6의 **실제 chunk
text/token count/anchor가 완전히 동일**함을 검증했다. 비교 matrix는 §6에 있다.
base의 128 실험을 추가하지 않아 size × model의 전체 상호작용은 추정하지 않는다.

## 5. Embedding Models

| 항목 | multilingual-e5-small | multilingual-e5-base |
|---|---|---|
| model name | intfloat/multilingual-e5-small | intfloat/multilingual-e5-base |
| family / 기반 | multilingual E5 / Multilingual MiniLM | multilingual E5 / XLM-R base |
| License | MIT | MIT |
| Dimension | 384 | 768 |
| 실제 parameter count | 117,653,760 (약 118M) | 278,043,648 (약 278M) |
| parameter FP32 bytes | 470,615,040 (약 449 MiB) | 1,112,174,592 (약 1,061 MiB) |
| query / document prefix | `query: ` / `passage: ` | 동일 |
| 모델 max input | 512 tokens (prefix·special 포함) | 동일 |
| 한국어/다국어 | 한국어 포함 100 languages | 동일 |
| 실행 자원 | 로컬 CPU float32, GPU 불필요 | 같은 CPU 실행 가능, 더 큰 비용 |

e5-small은 기존 Search의 연결 baseline이다. base는 다른 계열·instruction·context length까지
동시에 바꾸지 않고 모델 용량 증가의 실익을 확인하기 위해 선택했다. 두 모델 모두 로컬 배포
가능하고 license를 확인할 수 있다. **외부 embedding API 호출은 없다.**

모델 설명·라이선스·encoding 기준은 모델 발행자의
[small model card](https://huggingface.co/intfloat/multilingual-e5-small),
[base model card](https://huggingface.co/intfloat/multilingual-e5-base),
[Multilingual E5 technical report](https://arxiv.org/html/2402.05672v1)를 확인했다.
parameter 수·bytes는 실제 로드한 모델에서 계산했다. 두 모델 모두 L2 normalization 후
cosine을 사용하고 `query:`/`passage:`를 한국어에도 적용했다.

재현 revision: small `614241f622f53c4eeff9890bdc4f31cfecc418b3`,
base `d128750597153bb5987e10b1c3493a34e5a4502a`.

## 6. Overall Results

R@k는 relevant 문서 집합에 대한 recall의 질의별 평균이다. MRR은 top10 내 첫 relevant의
역순위이며, nDCG@5는 primary gain 2 / other relevant gain 1이다.
**P@1은 선행 harness의 `primary_at_1`** 표기로, 일반적인 relevant precision@1과 다르다.
No-answer는 recall 분모에 넣지 않고 null 및 점수 진단으로 유지했다.

복수 정답 질의가 있어 이 라벨셋에서 R@1의 이론적 최대는 약 0.9018이다.
C4의 MRR=1은 첫 relevant가 모두 1위라는 뜻이며 primary가 모두 1위라는 뜻은 아니다.

| ID | Chunking | Size | Model | R@1 | R@5 | MRR | nDCG@5 | P@1 | 판정 |
|---|---|---:|---|---:|---:|---:|---:|---:|---|
| C1 | Fixed | 64 | small | 0.8304 | 0.9464 | 0.9515 | 0.9350 | 0.8929 | POOR |
| C2 | Paragraph | 64 | small | 0.8661 | 0.9911 | 0.9732 | 0.9736 | 0.9643 | GOOD |
| C3 | Paragraph+Table | 64 | small | 0.8661 | 0.9554 | 0.9694 | 0.9532 | 0.9286 | ACCEPTABLE |
| C4 | Fixed | 64 | base | 0.9018 | 0.9911 | 1.0000 | 0.9871 | 0.9643 | GOOD |
| C5 | Paragraph | 64 | base | 0.8661 | 0.9911 | 0.9821 | 0.9739 | 0.9286 | ACCEPTABLE |
| C6 | Paragraph+Table | 64 | base | 0.8661 | 0.9911 | 0.9821 | 0.9732 | 0.9286 | ACCEPTABLE |
| C7 | Fixed | 128 | small | 0.8661 | 0.9911 | 0.9762 | 0.9701 | 0.9286 | ACCEPTABLE |
| C8 | Paragraph | 128 | small | 0.8661 | 0.9911 | 0.9762 | 0.9701 | 0.9286 | ACCEPTABLE |
| C9 | Paragraph+Table | 128 | small | 0.8304 | 0.9911 | 0.9554 | 0.9537 | 0.8929 | POOR |

판정은 이 표 내 비교용이다. GOOD=P@1≥0.95 및 R@5≥0.98,
ACCEPTABLE=P@1≥0.90 및 R@5≥0.95, 나머지 POOR로 요약했다.
실제 서비스 release threshold나 사전 검정 기준은 아니다.

9개 설정은 모델 가중치를 실제 로드해 평가했다. C7/C8의 text·경계는 이 corpus에서
동일하므로 지표도 동일하다. 동일 결과를 독립적인 품질 증거 두 개로 세지 않는다.
임시 서버 종료 처리 보완 후 다시 실행한 결과에서도 9개 설정의 순위·지표는 동일했다.

선행 Search의 sentence baseline은 R@1 0.8661 / R@5 0.9643 / MRR 0.9821이었다.
C2의 R@5는 높아졌지만 MRR은 낮아졌다. 이전 결과를 무조건 개선했다고 주장하지 않는다.
실험 내 통제 비교는 동일 구조의 C1~C9에 한정한다.

## 7. Category Results

각 셀은 **R@5 / P@1**이다. 전체 12 category 원본은
[category_metrics.csv](../../artifacts/chunking-embedding-poc/category_metrics.csv)에 있다.

| Category (n) | C1 | C2 | C3 | C4 | C5 | C6 | C7/C8 | C9 |
|---|---|---|---|---|---|---|---|---|
| semantic (4) | .750/.750 | 1/.750 | .750/.750 | 1/1 | 1/.750 | 1/.750 | 1/1 | 1/.500 |
| morphology (3) | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| numeric (2) | 1/.500 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/.500 | 1/1 |
| similar_document (2) | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| partial_match (2) | .750/.500 | .875/1 | .875/.500 | .875/.500 | .875/.500 | .875/.500 | .875/.500 | .875/.500 |

semantic에서는 Q019「해킹당하면…」의 정답 DOC-012가 C1/C3 7위, C2 4위,
C4 1위다. base의 개선은 실제 관측됐지만 C5/C6에서는 Q018의 정답이 2위로 내려간다.
큰 모델이 모든 chunker에서 가장 좋은 것은 아니다.

numeric은 Q028「서버 증설 120,000,000원」에서 C1 2위, C2~C6 1위,
C7/C8 3위, C9 1위다. morphology와 similar_document는 모든 설정에서 잘 풀려
추가 모델 비용을 정당화하는 변별력이 없다. category당 2~4개인 결과를 일반화할 수 없다.

exact_title(3), keyword(2), spacing(3), typo(2), year(3), department(2)는
모든 설정에서 P@1=1 및 R@5=1이다. 단, 복수 relevant query의 R@1은 1 미만일 수 있다.
no_answer(2)도 모두 실제 검색했다. C2의 평균 top cosine은 0.8141, C4는 0.8203으로,
무답변 질의도 높은 점수가 나온다. 점수만으로 refusal threshold를 확정하지 않는다.

## 8. Chunk Statistics

모델 간 chunk가 동일하여 각 전략·크기당 한 행으로 표현한다.
토큰 percentile은 numpy의 linear percentile이다.

| Config | Chunks | 문서당 평균 | 평균 tokens | P50 | P95 | Min/Max | TABLE | <16 tokens |
|---|---:|---:|---:|---:|---:|---|---:|---:|
| C1/C4 Fixed64 | 34 | 1.70 | 42.76 | 61 | 64 | 3/64 | 0 | 8 (23.5%) |
| C2/C5 Paragraph64 | 34 | 1.70 | 42.56 | 54 | 62 | 9/63 | 0 | 4 (11.8%) |
| C3/C6 Table64 | 37 | 1.85 | 39.11 | 45 | 62 | 9/63 | 3 | 6 (16.2%) |
| C7/C8 Fixed/Paragraph128 | 20 | 1.00 | 72.35 | 71.5 | 88.3 | 59/94 | 0 | 0 |
| C9 Table128 | 26 | 1.30 | 55.65 | 62 | 81.25 | 12/88 | 3 | 2 (7.7%) |

상한 초과 chunk와 model input 초과는 **0**이었다. Fixed64의 작은 꼬리 chunk가 많고,
표 분리도 짧은 본문 조각을 만든다. Paragraph64는 같은 34개로 작은 조각을 절반으로 줄였다.

문서 중복 제거 전 raw top5에서 한 문서의 최대 점유는 Fixed/Paragraph64 **2개**,
Table64/128 **3개**였다. raw top5 내 평균 고유 문서는 각각 4.53, 4.47, 4.47,
Table128 4.77이다. Fixed/Paragraph128은 5개다. 실제 평가 순위는 **전체 chunk의 max
score를 먼저 문서별로 집계**하므로 문서 중복은 없다. 먼저 chunk top5만 자르고 문서를
dedup하는 방식과 혼동하면 안 된다. 원시 목록은 `chunk_retrieval.jsonl`에 저장했다.

## 9. Table-aware Findings

표 값을 직접 요구하는 기존 질의는 **Q022(2026 출장 일비), Q028(서버 증설 금액)** 두 개다.
Q027의 300,000,000원 총액은 본문에 있으므로 표 전용 gain으로 세지 않았다.

| 비교 | 표 subset R@5 | 표 subset P@1 | 전체 nDCG@5 |
|---|---:|---:|---:|
| C2 Paragraph64 small | 1.000 | 1.000 | .9736 |
| C3 Table64 small | 1.000 | 1.000 | .9532 |
| C5 Paragraph64 base | 1.000 | 1.000 | .9739 |
| C6 Table64 base | 1.000 | 1.000 | .9732 |
| C8 Paragraph128 small | 1.000 | .500 | .9701 |
| C9 Table128 small | 1.000 | 1.000 | .9537 |

128에서 table-aware는 Q028의 정답 3→1위라는 **국소 gain**이 있었다.
64에서는 B도 작은 표 전체를 한 블록으로 보존하므로 C의 추가 gain은 없었다.
C9는 다른 문서의 표 chunk가 semantic 질의의 경쟁 후보가 되어 Q018/Q019를 악화시켰다.
해당 질의의 정답 문서 자체는 표가 없고 chunk도 바뀌지 않았으므로, 이 하락을
정답 문서 내부의 fragmentation/dilution이라고 설명하면 틀린다.

**채택 판단: 독립 table-aware는 기본 off.** 일부 숫자 검색 향상이 전체 품질 및 복잡도
증가를 상쇄한다는 근거는 부족하다. 원본의 행·열 구조를 폐기하라는 결론은 아니다.
3개의 작은 synthetic 표만 있어 긴 표의 header 반복이 retrieval을 개선하는지는 미측정이다.
긴 표의 행 보존·헤더 반복과 oversized row 오류는 별도 단위 테스트로 검증했으며
그 테스트 데이터를 retrieval corpus에 섞지 않았다. 병합 셀·실제 HWP는 미검증이다.

## 10. Fragmentation / Dilution Cases

### Fragmentation: 원문 chunk를 확인한 대표 3건

아래 사례는 C1/C4에 실제 존재하는 **근거 보존 결함**이다. 모두 검색 순위 하락으로
관측됐다고 주장하지 않는다. document-level recall은 chunk 내 근거 완전성을 측정하지 않는다.

| 문서 / 위치 | Fixed64의 실제 절단 | Paragraph64 / Table64 | 검색 결과와 해석 |
|---|---|---|---|
| DOC-003 p3, chunk0→1 | `소프트웨어 라이선스 \| 90` / `,000,000원` | 90,000,000원과 행·헤더가 같은 chunk | Q028은 다른 서버 행을 묻기 때문에 이 절단을 직접 평가하지 못한다. 미라벨 행의 금액 완전성 결함 |
| DOC-007 p2, chunk0→1 | `식비 \|` / `일 35,000원을 지급한다.` | 항목·값·header가 같은 chunk | Q022 일비 검색은 성공해도 식비 증거가 완전하다는 뜻은 아니다 |
| DOC-011 p3, chunk0→1 | `30분 내 복구되지 않으면 팀장` / `에게 보고한다.` | 조건·보고대상·행위가 같은 chunk | Q017 조치 방법의 정답 문서는 1위지만 보고 절차의 문장이 불완전해진다 |

추가 확인: Paragraph64에서도 DOC-007의 **연도(p0~1)**와 **일비 값(p2)**은
별도 chunk다. Q022 문서 retrieval은 성공하지만 RAG에서 한 청크만 읽을 때 연도+값이
함께 있다는 보장은 없다. overlap으로 이 문제를 해결했다고 주장하지 않는다.
실제 문서의 기존 paragraph anchor를 유지하는 이유이며 RAG context 구성이 남은 결정이다.

### Dilution: 크기를 늘렸을 때의 변화

Q028에서 C2 DOC-003의 표+집행부서 chunk는 **53 tokens**, 정답 rank 1,
cosine **0.881629**다. C8의 문서 전체 **94 tokens**에는 제목·총예산·세부표·담당부서가
합쳐지고 cosine **0.832373**, rank 3으로 내려간다. 두 경쟁 문서 DOC-015/016은 이
비교에서 동일 텍스트다. 따라서 이 사례는 정답 chunk에 다른 내용이 섞이며 점수가 낮아진
**dilution과 일치하는 관측**이다. 짧은 synthetic 한 건으로 일반 인과 법칙을 정하지 않는다.

반대 방향도 있다. Q019에서 C2→C8은 4→1위로 좋아진다. 정답 DOC-012는 두 설정에서
동일한 59-token chunk이며 cosine도 **0.801120**으로 같다. 경쟁 문서 DOC-014/016/017의
짧은 부분이 전체 문서 chunk로 합쳐지며 오탐 경쟁력이 낮아진 결과다.
큰 chunk가 항상 정답 context를 개선한다는 해석도 피해야 한다.

**256 vs 512 답변:** 직접 비교하지 않았다. 이 데이터는 구조화 후에도 최대 94 tokens여서
두 설정이 같은 경계를 만든다. 대체한 64/128 비교는 semantic·numeric의 tradeoff를 보여줬지만
실제 장문 256/512의 성능을 추정할 근거는 아니다.

## 11. Embedding Performance

Linux x86_64, Intel Core Processor (Haswell, IBRS), visible CPU 8개, 물리 메모리 약 7.8 GiB.
실제 inference는 torch intra-op **2 threads**, inter-op **1**, float32, CPU로 고정했다.
Python 3.10.12, torch 2.14.0+cpu, sentence-transformers 6.0.1 등 상세 버전은 manifest에 있다.

| 항목 | e5-small (C2 corpus) | e5-base (C4 corpus) |
|---|---:|---:|
| Dimension | 384 | 768 |
| Model load (로컬 cache, import 제외) | 2.717 s | 2.579 s |
| Corpus 20 docs / 34 chunks embedding | 1.680 s | 6.488 s |
| Chunk 평균 (batch amortized) | 49.42 ms | 190.82 ms |
| 문서 평균 (batch amortized) | 84.01 ms | 324.39 ms |
| 단건 query embedding P50 | 31.50 ms | 92.77 ms |
| 단건 query embedding P95 | 39.00 ms | 109.83 ms |
| Peak process RSS | 893.64 MiB | 1150.93 MiB |

Corpus는 warm-up 후 설정당 한 번, query는 30개 전체를 3회(90 observations/model)
측정했다. no_answer도 시간 측정에 포함했다. corpus 시간에는 토큰 수 검사와 출력 검증도
포함된다. batch 평균은 단건 API latency가 아니다. query vector를 cache해서 DB를
평가했으므로 `metrics.csv.latency_ms_*`는 encoding 비용을 제외한다.

모델별 새 프로세스로 분리했고 RSS는 라이브러리·tokenizer·모델·해당 모델의 모든 설정이
포함된 Linux high-water 값이다. 가중치 mmap·OS page cache 및 접근한 embedding row의
영향이 있어 모델의 전체 resident RAM 요구량이나 배포 메모리 예약값과 같지 않다.
load time도 cold disk 측정이 아니므로 base가 더 빨리 로드된다고 일반화하지 않는다.
CPU 부하가 다른 환경·실제 대용량 corpus의 throughput·동시 요청 비용은 추정하지 않는다.

## 12. RRF Recheck

검색 품질 우승 **C4만** 기존 pg_trgm과 결합했다. lexical은 원본 corpus,
floor=0.20, RRF k=60, candidate_k=10, 질의별 weight/parameter 변경은 없다.

| 방식 | R@1 | R@5 | MRR | nDCG@5 | P@1 |
|---|---:|---:|---:|---:|---:|
| C4 vector only | .9018 | .9911 | 1.0000 | .9871 | .9643 |
| C4 + pg_trgm RRF | .9018 | .9911 | 1.0000 | .9852 | .9643 |

**이번에는 Top-1 악화도 개선도 없었다.** 28개 answerable의 primary rank1 결과가 모두
같았고, Q004/Q027에서 secondary relevant DOC-002가 3→4위로 내려가 nDCG@5가
각각 .950234→.923885로 낮아졌다. 최종 RRF가 vector 단독보다 이득이라는 근거는 없다.
Q014 최신 연도 오순위도 해결하지 못했다.

C2에는 RRF를 추가 평가하지 않았다. 그러므로 **C2에서도 RRF가 반드시 악화된다**고
주장하지 않는다. 구현 기본 fusion을 off로 두는 판단은 C4 재검증에 gain이 없고
기존 Search에서도 기본 결합을 정당화하지 못했다는 제한적 근거에 따른다.
pg_trgm은 독립 lexical 경로 후보로 유지한다.

## 13. Recommendation

```text
PROVISIONAL PRODUCTION CANDIDATE — C2

Chunking:    Paragraph-aware
Target size: 64 tokens (hard max 64, 이번 짧은 corpus 기준의 잠정값)
Overlap:     0
Table-aware: OFF (독립 표 분리/헤더 반복 기본 비활성)

Embedding:   intfloat/multilingual-e5-small
Dimension:   384

Lexical:     pg_trgm (기존 floor 0.20, 별도 lexical 경로 후보)
Fusion:      vector-only 기본, 자동 RRF OFF
```

선정 이유는 C2의 P@1 **27/28**, R@5 **0.9911**이 C4와 같으면서,
query P95가 약 **2.8배**, corpus 비용이 약 **3.9배** 낮고 vector payload가 절반이기 때문이다.
§10의 숫자·행·문장 절단 3건도 C2는 보존한다. 이 선택은 C4보다 MRR .0268,
nDCG .0135가 낮고 Q019가 4위라는 비용을 받아들인다. 품질 손실을 감추지 않는다.
최고 숫자 한 개보다 근거 보존·CPU 비용·384d 운영 단순성을 함께 고려했다.

반드시 답해야 할 질문의 판정:

| 질문 | 명시적 답변 |
|---|---|
| Fixed보다 paragraph가 실제로 좋은가? | small64에서는 P@1 .8929→.9643, R@5 .9464→.9911 및 경계 보존 개선. base64에서는 오히려 P@1 .9643→.9286. 보편적 우위는 없음 |
| Table-aware 복잡도를 감수할 gain인가? | 128의 Q028에서는 gain, 64에서는 추가 gain 없음. 전체 품질 하락 사례를 감안해 기본 off |
| 256 vs 512 영향은? | 짧아서 직접 변별 불가. 대체 64→128은 semantic 개선·numeric 악화가 공존. 장문 최적값 미확정 |
| e5-small보다 유의미하게 좋은 후보인가? | 동일 Fixed64에서 base는 P@1 +2/28, Q019 7→1위, Q028 2→1위로 관측상 개선. 28개 질의로 통계적 유의성/회사 우위는 입증하지 않음 |
| 큰 모델의 비용 증가가 가치 있는가? | 이번 기본값에는 채택하지 않음. C2와 C4의 P@1/R@5가 같고 base의 비용·Fixed 근거 절단을 감수할 전체 이득이 부족하다고 판단 |
| DB dimension 후보는? | 구현 기본 C2에 맞춰 **384**. 768은 실험한 대안이며 우선 DB 후보가 아님 |
| 개선 후 RRF가 이득인가? | C4에서 R@1/R@5/MRR/P@1 동일, nDCG만 소폭 하락. 기본 적용 근거 없음. C2 RRF는 미측정 |

이 결정은 **FINAL PRODUCTION OPTIMUM이 아니다**. 실제 corpus를 확보하면 동일한
평가 계약으로 다시 검증한다. 현재 64가 사내 문서용 최적 chunk size라는 뜻도 아니다.

## 14. Architecture Impact

다음은 **제안만** 남긴다. 기존 기능명세/DB v2.3 파일과 production DB는 수정하지 않았다.

- DB `chunks.embedding VECTOR(1536)`을 구현 기본 모델에 맞춰 **VECTOR(384)**로 반영 제안.
  모델 교체 시 재임베딩 및 dimension migration 필요성은 기존 설계를 따른다.
- 기능명세 §8.6과 DB §28의 미확정 chunking을 Paragraph64/overlap0 잠정 정책으로 명시 제안.
  source `paragraph_start/end`와 `chunking_version`, 모델 revision·prefix·dimension을 함께 기록한다.
- `content_type` 신규 DB 컬럼/전용 table schema는 현재 gain 근거로 추가하지 않는다.
  PoC 내부 TEXT/TABLE 값만 사용한다. 기존 parser row/column/cell 구조는 유지한다.
- DB exact vector + 문서별 max-score dedup을 유지하고, 기본 RRF 비활성 및 pg_trgm 별도 경로를
  기능명세에 반영 제안한다. HNSW, RAG Top-K, refusal은 이번 결정에 포함하지 않는다.

Dimension별 float32 **순수 좌표 payload** 추정 (decimal MB/GB):

| dimension | vector 1개 | 100k chunks | 1M chunks |
|---:|---:|---:|---:|
| 384 | 1,536 B | 153.6 MB | 1.536 GB |
| 768 | 3,072 B | 307.2 MB | 3.072 GB |
| 1024 | 4,096 B | 409.6 MB | 4.096 GB |

계산은 `4 × dimension × chunk_count`다. pgvector의 `vector` 값 자체는 좌표 이외에
8 bytes를 더 사용한다는 [공식 문서](https://github.com/pgvector/pgvector#vector-type)를
참고할 수 있다. 표는 그 8 bytes도 제외한다. PostgreSQL row/TOAST/index/WAL/복제/
백업·동시 revision의 비용을 포함한 production sizing이 아니다.

## 15. Remaining Decisions

남은 것은 실제 사내 corpus 확보 후 재검증, 장문·긴 표·병합 셀 및 실제 paragraph anchor
적용 확인, HNSW 적용 시점, RAG Top-K/인접 근거 조합, refusal threshold,
동시 요청 기준의 메모리·latency 예산이다. 이번 보고서에서 수치나 구현을 확정하지 않는다.

대표 검색 실패는 5개 이내로 정리하면 다음과 같다.

1. **Q019**: 기본 후보 C2는 보안사고 문서를 4위에 반환한다. base Fixed64에서는 1위다.
2. **Q014**: 지표 우승 C4는 최신 2026 출장비보다 2025를 앞세운다. RRF도 동일하고 C2는 2026이 1위다.
3. **Q013**: C4는 개인정보 관련 정답 4개 중 교육 문서 DOC-013을 top10 밖에 놓친다. RRF는 6위로 올리지만 top5에는 못 든다. C2도 DOC-013은 7위다.
4. **Q028**: C8의 큰 chunk는 서버 증설 금액 문서를 3위로 낮춘다. Paragraph64/표 분리에서 1위다.
5. **Q031/Q032**: 정답 없는 두 질의에도 고득점 문서를 반환한다. retrieval 검증이며 refusal 성공으로 세지 않았다.

완료 확인: 원본 dataset 재사용, A/B/C 평가, 두 모델 및 e5-small baseline,
청크 통계, R@1/R@5/MRR/nDCG@5/P@1, 12 category, fragmentation 3건,
dilution·표 분석, CPU latency/peak RSS, best RRF, 잠정 후보, limitation,
보고서·재현 artifact를 모두 작성했다. 기능명세/DB 직접 수정 및 자동 commit은 하지 않았다.
실제 HWP fixture 부재로 기존 parser integration tests는 skip되며 이를 이번 검색 PoC의
실제 사내 검증으로 계산하지 않는다.

최종 검증은 **pytest 208 passed / 33 skipped**, 변경 코드 pyflakes 통과,
`git diff --check` 통과다. 저장된 300개 query 결과에서 지표를 재계산했고,
모델 쌍의 chunk 일치·청크 통계·입력 및 실행 코드 해시·strict JSON 형식도 확인했다.
결과 artifact는 10개, 총 약 466 KB이며 재생성 가능한 embedding binary는 없다.

**Conclusion: PASS WITH LIMITATIONS.** 새로운 일반 PoC를 후속 목표로 추가하지 않는다.
다음 단계는 **설계 결과 반영 → 설계 Freeze → API Contract → DB Migration → 본 서비스 구현**이다.
실제 사내 corpus 검증은 데이터가 확보될 때 반드시 수행할 검증 조건으로 유지한다.
