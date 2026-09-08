# 한국어 검색 PoC 보고서 (Synthetic Corpus)

* 작성일: 2026-09-08
* 대상 질문: **실제 사내 corpus를 받기 전 단계에서, 어떤 검색 조합을 MVP의 우선 후보로 가져갈 것인가?**
* 선행 문서: `docs/functional-spec-v2.3.md`, `docs/database-schema-v2.3.md`, `docs/poc/internal-hwp-corpus-report.md`
* 코드: `poc/search/` · 결과: `artifacts/search-poc/`
* 범위: 검색 방식 비교만. FastAPI / RAG / ACL / 실제 ingestion은 구현하지 않았다.

---

## 1. Executive Summary

### 결론: **PASS WITH LIMITATIONS**

통제된 한국어 검색 failure mode에서 네 가지 방식을 비교했고, 각 방식이 **어떤 문제를 해결하고
어떤 문제를 해결하지 못하는지** 명확히 분리되었다. MVP 우선 후보를 정할 근거는 확보했다.

`WITH LIMITATIONS`인 이유는 §2다 — **synthetic corpus는 사내 문서를 대표하지 않는다.**

### 한 문장 요약

| 방식 | 이 실험에서 관찰된 성격 |
| --- | --- |
| `simple` FTS (AND) | **단독 사용 불가.** R@5 0.30 |
| `simple` FTS (OR) | AND를 OR로 바꾸는 것만으로 R@5 0.30 → 0.80. 남은 실패는 형태소·의미 |
| pg_trgm | 띄어쓰기·오타·연도·부분일치를 **전부** 해결. 의미 검색은 불가 (R@5 0.25) |
| Vector | 의미·형태소 변화를 **전부** 해결. 유사 문서 구분에서 약점 |
| RRF Hybrid | 이 데이터에서는 **Vector 단독을 넘어서지 못했다** |

### 이 실험에서 가장 중요한 관찰 세 가지

1. **`plainto_tsquery`의 AND 결합이 한국어 검색을 망가뜨리는 주범이다.**
   조사가 붙은 토큰 하나가 매칭되지 않으면 문서 전체가 탈락한다. OR로만 바꿔도 R@5가 0.30 → 0.80이 된다.
   이것은 형태소 분석기 도입 이전에 확인해야 할 **설정 문제**다.

2. **RRF Hybrid가 항상 이기지는 않는다.**
   Vector 단독 P@1 0.964 vs Hybrid 0.893. 구성 요소 간 실력 차가 크면 RRF의 동등 가중이 오히려 손해다.
   "Hybrid가 가장 안정적"이라는 통념은 이 데이터에서 성립하지 않았다.

3. **Vector 유사도는 no-answer를 걸러내지 못한다.**
   정답이 없는 질의에도 cosine 0.816을 준다(정답 있는 질의 평균 0.906, 차이 0.09).
   반면 lexical 방식은 **결과를 아예 반환하지 않는다.** RAG의 "근거 부족" 판정(기능명세 §13.2)에
   직접적인 영향이 있다.

---

## 2. Scope — 이 결과로 주장할 수 없는 것

> **Synthetic corpus는 실제 사내 문서를 대표하지 않는다.**

현재 알 수 없는 것:

```text
실제 문서 제목 스타일        실제 부서 약어
실제 업무 용어               실제 문서 길이
실제 표 비율                 실제 버전/중복 분포
실제 사용자 검색어
```

따라서 다음과 같은 주장은 **하지 않는다.**

```text
"실제 사내 문서에서도 Recall@5가 0.96이다."
"이 검색 방식이 production에서 확실히 최고다."
```

이 PoC가 지지할 수 있는 결론은 이 수준이다.

```text
"통제된 한국어 검색 failure mode에서는
 A 방식보다 B 방식이 더 안정적으로 동작했다."
```

문서 20건 / 질의 30건은 **방식 간 순위를 비교하기 위한 규모**이지 절대 성능을 측정하는 규모가 아니다.
category당 질의가 2~4개이므로 **category별 수치는 질의 1개 차이로 크게 흔들린다.**
category 표는 절대값이 아니라 **패턴**으로 읽어야 한다.

---

## 3. Dataset

| 항목 | 값 |
| --- | --- |
| Documents | 20 (가상 조직 7개 부서, 2025~2026) |
| Chunks | 108 (title + 문장 단위 baseline) |
| Queries | 30 (답변 가능 28 + no-answer 2) |
| Categories | 12 |

`poc/search/fixtures/documents.jsonl`, `poc/search/fixtures/queries.jsonl`

### 검색이 쉬워지지 않도록 한 설계

질의의 핵심 단어가 정답 문서에만 존재하면 어떤 방식이든 만점이 나온다. 이를 막기 위해
문서 간 핵심어를 의도적으로 겹치게 만들었다.

```text
"AI 문서관리"       DOC-001~005, DOC-020   연도·문서유형만 다른 6건
"예산"              6개 문서
"시스템 운영지침"    DOC-008(정보보안팀) / DOC-009(AI플랫폼팀)  부서만 다름
"출장비"            DOC-006(2025) / DOC-007(2026)             연도만 다름
"교육 운영계획"      DOC-013(2026) / DOC-018(2025)             연도만 다름
"120,000,000원"     DOC-003(서버 증설) / DOC-016(정보보안 개선) 같은 금액, 다른 맥락
```

`spacing` / `typo` / `morphology` / `semantic` 질의는 정답 문서에도 질의 문자열이 그대로
등장하지 않는다 — 어휘 일치로 풀리면 안 되는 유형이기 때문이다.

### Category 분포

| Category | n | Category | n |
| --- | --: | --- | --: |
| exact_title | 3 | year | 3 |
| keyword | 2 | department | 2 |
| spacing | 3 | numeric | 2 |
| typo | 2 | similar_document | 2 |
| partial_match | 2 | semantic | 4 |
| morphology | 3 | no_answer | 2 |

---

## 4. Methods

전 질의에 **동일한 고정 설정**을 적용했다. 질의별 threshold·가중치 조정은 하지 않았다.

| Method | 구현 | 고정 파라미터 |
| --- | --- | --- |
| `fts_simple` | `to_tsvector('simple')` + `plainto_tsquery` (**AND**) | `ts_rank_cd` |
| `fts_simple_or` | 같은 tsvector + `to_tsquery`로 **OR** 결합 | `ts_rank_cd` |
| `trigram` | `GREATEST(word_similarity(q,title), word_similarity(q,content))` | floor `0.20` 전 질의 공통 |
| `vector` | chunk 임베딩 + pgvector cosine, 문서 점수 = best chunk | — |
| `rrf_fts_vector` | RRF(FTS-AND, Vector) | `k = 60` |
| `rrf_fts_trgm_vector` | RRF(FTS-AND, trigram, Vector) | `k = 60` |
| `rrf_fts_trgm` | RRF(FTS-AND, trigram) | `k = 60` |
| **PGroonga** | **NOT TESTED** | §4.1 |

### 왜 `fts_simple_or`를 추가했나

`plainto_tsquery`는 토큰을 AND로 묶는다. 한국어에서는 조사가 붙은 토큰이 별개 토큰이 되므로
토큰 하나만 어긋나도 문서가 통째로 탈락한다. OR 변형을 함께 측정하지 않으면
**"AND 결합 때문에 실패한 것"과 "토큰화 때문에 실패한 것"을 구분할 수 없다.**
두 원인은 해법이 전혀 다르므로(설정 변경 vs 형태소 분석기 도입) 분리해서 측정했다.

### 4.1 PGroonga

```text
NOT TESTED
Reason: environment / operational overhead
```

구체적으로: 이 환경에는 root 권한이 없고 PGroonga는 Groonga C 라이브러리(`libgroonga-dev`)를
요구한다. 배포판 패키지 설치가 불가능하고, Groonga를 소스에서 빌드하려면 형태소 분석기(MeCab) 등
추가 의존성 체인을 함께 빌드해야 한다.

**PGroonga 검증 때문에 PoC를 지연시키지 않았다**(요청 §24).
PGroonga는 한국어 형태소 색인을 제공하므로 `morphology` / `spacing` category에서
`simple` FTS보다 크게 나을 가능성이 높다. **사내 corpus 확보 시 재평가 대상이다.**

### 4.2 Embedding model

```text
intfloat/multilingual-e5-small
384 dim · 118M params · MIT · CPU 실행 · 다국어(한국어 포함)
```

> **PoC용 embedding model이며 production 확정이 아니다.**

선정 사유는 §17 조건(한국어 지원 / 로컬 실행 / 라이선스 확인 가능 / GPU 비의존)을 만족하는
모델 **하나를 고른 것**이지, 후보 간 비교를 거친 결과가 아니다.
사내 데이터 보안정책이 확정되지 않았으므로 외부 embedding API는 기본 후보로 쓰지 않았다.

### 4.3 Chunking

title을 chunk 0으로 두고 본문을 문장 단위로 나눴다. 문서 점수는 best matching chunk다.

> **최종 chunking strategy가 아니다.** 현재 목적은 검색 방식 비교이며,
> chunking 정책은 사내 corpus 측정 이후 별도로 결정한다(기능명세 v2.3 §8.6).

---

## 5. Overall Metrics

답변 가능한 28개 질의 기준. `P@1` / `P@5` = primary_document가 1위 / top-5 안에 든 비율.

| Method | R@1 | R@5 | MRR | nDCG@5 | P@1 | P@5 | p50 (ms) | p95 (ms) |
| --- | --: | --: | --: | --: | --: | --: | --: | --: |
| `fts_simple` | 0.277 | 0.304 | 0.321 | 0.307 | 0.286 | 0.321 | 0.2 | 0.4 |
| `fts_simple_or` | 0.634 | 0.804 | 0.762 | 0.761 | 0.679 | 0.821 | 0.4 | 0.7 |
| `trigram` | 0.759 | 0.857 | 0.857 | 0.846 | 0.821 | 0.857 | 1.2 | 1.7 |
| **`vector`** | **0.866** | **0.964** | **0.982** | **0.970** | **0.964** | **1.000** | 27.9 | 80.2 |
| `rrf_fts_vector` | 0.848 | 0.964 | 0.964 | 0.955 | 0.893 | 1.000 | 27.2 | 49.7 |
| `rrf_fts_trgm_vector` | 0.848 | 0.964 | 0.964 | 0.955 | 0.893 | 1.000 | 29.1 | 40.7 |
| `rrf_fts_trgm` | 0.741 | 0.857 | 0.839 | 0.836 | 0.786 | 0.857 | 1.4 | 2.0 |

**Vector 단독이 모든 지표에서 1위이며, 어떤 Hybrid 조합도 이를 넘지 못했다.**

### No-answer 질의 (2건)

정답이 corpus에 없는 질의에서 top-1 점수가 정답 있는 질의와 구분되는지 관찰했다.

| Method | 정답 있는 질의 top-1 평균 | no-answer top-1 평균 | 분리도 |
| --- | --: | --: | --: |
| `fts_simple` | 0.133 | **결과 없음** | 명확 |
| `fts_simple_or` | 0.530 | 0.100 | +0.430 |
| `trigram` | 0.748 | **결과 없음** | 명확 |
| `vector` | 0.906 | 0.816 | **+0.091** |
| `rrf_fts_vector` | 0.022 | 0.016 | +0.006 |

`vector`는 존재하지 않는 답에도 0.816을 부여한다.
`Q032 "임직원 자녀 학자금 신청 방법"` → `DOC-008 정보보안팀 시스템 운영지침` (0.815).
**cosine 임계값으로 "근거 없음"을 판정할 수 없다.**

lexical 방식은 같은 질의에서 결과를 0건 반환한다 — 훨씬 신뢰할 수 있는 refusal 신호다.

---

## 6. Category Metrics

### Recall@5

| Category | n | fts(AND) | fts(OR) | trigram | vector | rrf(f+v) | rrf(f+t+v) | rrf(f+t) |
| --- | --: | --: | --: | --: | --: | --: | --: | --: |
| exact_title | 3 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| keyword | 2 | 0.50 | 1.00 | 1.00 | 0.75 | 0.75 | 0.75 | 1.00 |
| spacing | 3 | **0.00** | 0.67 | **1.00** | 1.00 | 1.00 | 1.00 | 1.00 |
| typo | 2 | **0.00** | 1.00 | **1.00** | 1.00 | 1.00 | 1.00 | 1.00 |
| partial_match | 2 | 0.75 | 0.75 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| morphology | 3 | **0.00** | 0.33 | 0.67 | **1.00** | 1.00 | 1.00 | 0.67 |
| semantic | 4 | **0.00** | 0.50 | **0.25** | **1.00** | 1.00 | 1.00 | 0.25 |
| year | 3 | **0.00** | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| department | 2 | 0.50 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| numeric | 2 | 0.50 | 1.00 | 1.00 | 0.75 | 0.75 | 0.75 | 1.00 |
| similar_document | 2 | 0.50 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |

### primary_at_1 (정확히 원하는 문서를 1위로 올렸는가)

| Category | n | fts(AND) | fts(OR) | trigram | vector | rrf(f+v) | rrf(f+t) |
| --- | --: | --: | --: | --: | --: | --: | --: |
| exact_title | 3 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| keyword | 2 | 0.50 | 0.50 | **1.00** | **1.00** | **0.50** | **0.50** |
| spacing | 3 | 0.00 | 0.67 | 1.00 | 1.00 | 1.00 | 1.00 |
| typo | 2 | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| partial_match | 2 | 0.50 | 0.50 | 0.50 | **1.00** | **0.50** | **0.50** |
| morphology | 3 | 0.00 | 0.00 | 0.67 | 1.00 | 1.00 | 0.67 |
| semantic | 4 | 0.00 | 0.50 | 0.25 | 1.00 | 1.00 | 0.25 |
| year | 3 | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| department | 2 | 0.50 | 0.50 | 1.00 | 1.00 | 1.00 | 1.00 |
| numeric | 2 | 0.50 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| similar_document | 2 | 0.50 | 1.00 | 1.00 | **0.50** | **0.50** | **1.00** |

굵게 표시한 칸이 방식 간 성격 차이가 드러나는 지점이다.

> ⚠️ category당 질의가 2~4개다. `0.50`은 "2개 중 1개"라는 뜻이며,
> 이 수치를 정밀한 성능차로 읽으면 안 된다.

---

## 7. Representative Queries

`primary_document`의 순위. `-` = top-10 밖.

| QID | Category | 질의 | fts(AND) | fts(OR) | trigram | vector | rrf(f+v) |
| --- | --- | --- | --: | --: | --: | --: | --: |
| Q009 | spacing | 보안사고대응절차 | – | – | **1** | 1 | 1 |
| Q010 | typo | 개인정보보호 처리지친 | – | 1 | **1** | 1 | 1 |
| Q016 | morphology | 개인정보를 파기하는 절차는 어떻게 되나 | – | – | **1** | 1 | 1 |
| Q017 | morphology | 서버에 장애가 났을 때의 조치 방법 | – | – | – | **1** | 1 |
| Q018 | semantic | 시스템이 다운되면 누구한테 연락해야 하지? | – | – | – | **1** | 1 |
| Q019 | semantic | 해킹당하면 어디에 알려야 하나요 | – | – | – | **1** | 1 |
| Q004 | keyword | 문서관리 구축 예산 | – | 2 | **1** | **1** | **2** |
| Q014 | partial_match | 출장비 | 2 | 2 | 2 | **1** | **2** |
| Q029 | similar_document | AI 문서관리 사업계획 전사 확대 | – | 1 | **1** | **2** | 2 |
| Q031 | no_answer | 사내 워케이션 지원금은 얼마인가? | 결과없음 | 오답 0.10 | 결과없음 | **오답 0.82** | 오답 |

### 요구된 대비 사례 (요청 §23)

**FTS만 잘한 Query — 없음.**
`fts_simple` / `fts_simple_or`가 다른 방식보다 나은 질의는 **하나도 없었다.**
FTS가 맞힌 질의는 trigram과 vector도 전부 맞혔다.

**trigram이 개선한 Query — Q004, Q009, Q015, Q016, Q026**
* `Q009 "보안사고대응절차"` — 문서에는 `보안사고 대응 절차`(띄어씀). 어떤 FTS 변형도 못 찾는다.
  trigram은 문자 3-gram이라 띄어쓰기를 넘는다.
* `Q016 "개인정보를 파기하는 절차는 어떻게 되나"` — 조사가 붙어 FTS 토큰이 전부 어긋난다.

**Vector만 잘한 Query — Q014, Q017, Q018, Q019**
* `Q018 "시스템이 다운되면 누구한테 연락해야 하지?"` → `DOC-011 서버 장애 대응 매뉴얼`.
  질의와 문서가 **공유하는 단어가 사실상 없다.** lexical 방식은 원리적으로 불가능하다.
* `Q014 "출장비"` — DOC-006(2025)/DOC-007(2026) 중 최신을 1위로 올린 것은 vector뿐이다.

**Hybrid가 개선한 Query — 없음.**
모든 구성 요소보다 Hybrid가 나은 질의는 **하나도 없었다.**

**Hybrid가 악화한 Query — Q004, Q014**
* `Q004 "문서관리 구축 예산"` — trigram 1위, vector 1위, **RRF 2위.**
  두 방식이 서로 다른 문서를 1위로 올려 RRF가 절충하면서 정답이 밀렸다.
* `Q014 "출장비"` — vector 1위(DOC-007), trigram 1위는 DOC-006. RRF는 DOC-006을 택했다.

**No-answer Query — Q031, Q032**
lexical은 결과 0건. vector는 0.82의 높은 유사도로 무관한 문서를 1위에 올린다.

### 유사 문서 구분에서의 Vector 약점

`Q029 "AI 문서관리 사업계획 전사 확대"` (정답 DOC-002 / 2026)

```text
trigram   DOC-002, DOC-001, DOC-020     정답 1위
vector    DOC-001, DOC-002, DOC-004     정답 2위  ← 2025년 문서를 위로 올림
```

`전사 확대`는 DOC-002에만 있는 단서다. 임베딩은 두 문서를 거의 같은 의미로 보고
결정적 단서를 희석시켰다. **lexical이 필요한 이유가 여기 있다.**

---

## 8. Findings

### simple FTS만으로 충분한가?

**충분하지 않다.** 다만 실패 원인이 두 층으로 나뉜다.

```text
AND 결합 문제   R@5 0.30 → 0.80   OR로 바꾸면 해결
토큰화 문제     R@5 0.80 에서 정체  OR로도 해결 안 됨
```

OR로 바꾼 뒤에도 남는 실패는 `morphology`(0.33), `semantic`(0.50), `spacing`(0.67),
`partial_match`(0.75)이다. 이는 `simple`이 형태소를 모르고 어절을 통째로 토큰화하기 때문이며
설정으로는 해결되지 않는다.

> 실무적 함의: 한국어 FTS를 도입한다면 **`plainto_tsquery`를 그대로 쓰면 안 된다.**
> 이것만으로 R@5가 2.6배 차이 난다.

### pg_trgm이 실제로 보완하는 문제는 무엇인가?

```text
띄어쓰기       0.00 → 1.00   (FTS-AND 대비)
오타           0.00 → 1.00
연도 구분      0.00 → 1.00
부분 문자열    0.75 → 1.00
형태소 변화    0.00 → 0.67
```

문자 n-gram이라 어절 경계와 무관하게 동작한다. **한국어 lexical 검색의 실질적 baseline은
FTS가 아니라 trigram이다.**

보완하지 못하는 것: `semantic` 0.25. 어휘가 겹치지 않으면 원리적으로 불가능하다.

### Vector가 semantic search에서 유의미한가?

**명확히 유의미하다.** `semantic` R@5: trigram 0.25 → vector 1.00, `morphology` 0.67 → 1.00.

`Q018`처럼 질의와 문서가 단어를 공유하지 않는 경우는 lexical로 도달할 수 없다.
사용자가 자연어로 묻는 RAG 환경에서는 이 유형이 드물지 않을 것이다.

### Vector가 exact/numeric 검색을 악화시키는가?

**전면적으로 악화시키지는 않았지만 약점이 있다.**

```text
exact_title    1.00   악화 없음
numeric        0.75   trigram 1.00 대비 낮음
keyword        0.75   trigram 1.00 대비 낮음
similar_document primary_at_1 0.50   trigram 1.00 대비 낮음
```

`Q028 "서버 증설 120,000,000원"`처럼 **같은 금액이 다른 맥락에 등장**하거나,
`Q029`처럼 거의 동일한 두 문서를 단서 하나로 구분해야 할 때 임베딩은 흐려진다.

### RRF Hybrid가 가장 안정적인가?

**이 데이터에서는 아니다.**

```text
vector          R@5 0.964  MRR 0.982  P@1 0.964
rrf_fts_vector  R@5 0.964  MRR 0.964  P@1 0.893   ← 더 낮음
```

Hybrid가 모든 구성 요소보다 나은 질의는 0건, 악화시킨 질의는 2건이었다.

원인은 구성 요소 간 실력 차다. RRF는 순위만 보고 **동등 가중**으로 합치므로,
한쪽이 크게 약하면 강한 쪽의 정답을 끌어내린다. `Q004`, `Q014`가 그 사례다.

다만 Hybrid에는 지표에 안 잡히는 장점이 있다.

* **Recall은 잃지 않는다** — R@5 0.964로 vector와 동일하고 `keyword`/`numeric`에서
  vector가 놓친 문서를 lexical이 보완한다.
* **단일 방식 실패에 대한 보험** — 실제 사내 문서에서 임베딩 모델이 업무 용어를 못 다룰 경우,
  lexical 경로가 남아 있다.

> 이 데이터셋은 lexical 성분이 약하도록 구성되지 않았음에도(trigram R@5 0.857)
> RRF가 이득을 내지 못했다. **사내 corpus에서 재확인이 필요한 지점이다.**

---

## 9. Recommendation

```text
PROVISIONAL RECOMMENDATION

Lexical:
    pg_trgm 를 lexical 기본으로 사용한다.
    simple FTS 를 함께 쓴다면 plainto_tsquery(AND) 대신 OR 결합을 사용한다.

Vector:
    다국어 임베딩 + pgvector.
    모델은 미확정. 사내 보안정책 확정 후 후보 비교를 별도로 수행한다.

Fusion:
    RRF (k=60) 를 유지하되, Vector 단독 대비 이득이 없을 수 있음을 전제한다.
    구성 요소 가중을 도입할지는 사내 corpus에서 결정한다.
```

### 근거

* `trigram`은 한국어 lexical의 실질 baseline이다. `simple` FTS 단독은 어떤 설정에서도 최하위였다.
* `vector`는 semantic/morphology를 **유일하게** 해결하고 전 지표 1위였다.
* `RRF`는 이 데이터에서 이득이 없었지만, **잃는 것도 R@5 기준 없다.**
  단일 방식 실패에 대한 보험 가치가 있으므로 구조는 유지하고, 실제 데이터에서 재평가한다.

### 반드시 함께 기록할 것

> **실제 사내 corpus에서 재검증 후 확정한다.**

특히 다음은 이 실험으로 답할 수 없다.

* 사내 업무 용어·부서 약어에서 임베딩 모델이 작동하는가
* 실제 문서 길이(수십 페이지)에서 best-chunk aggregation이 유효한가
* PGroonga 형태소 색인이 trigram보다 나은가

---

## 10. Architecture Impact

기존 문서를 **직접 수정하지 않았다.** 아래는 제안이다.

### 10.1 기능명세 v2.3 §11.4 — 한국어 Lexical Search PoC 후보 갱신 제안

현재 명세의 후보:

```text
A. PostgreSQL 기반 기본 lexical 검색
B. PostgreSQL + trigram
C. 별도 한국어 검색 확장 + pgvector
```

이번 결과를 반영한다면 A를 **baseline이 아니라 반례**로 기록하고,
`plainto_tsquery` AND 결합 문제를 명시하는 것이 유용하다.
PGroonga는 미검증이므로 후보에서 제거하지 않는다.

### 10.2 기능명세 v2.3 §13.2 — 근거 부족 처리에 영향 (중요)

명세는 "검색 결과 없음 / 근거 없음"일 때 답변을 제한하도록 요구한다.

이번 결과는 **vector 유사도 임계값만으로는 이 판정이 불가능**함을 보여준다
(no-answer 0.816 vs answerable 0.906).

제안: refusal 판정에 **lexical 결과 존재 여부를 함께 사용**한다.
확정은 RAG 평가 단계에서 하되, 명세에 "vector score 단독 임계값에 의존하지 않는다"를
남겨두는 것이 안전하다.

### 10.3 DB 스키마 v2.3 — 변경 불필요

```text
NO SCHEMA CHANGE REQUIRED
```

`chunks.search_vector TSVECTOR`와 `chunks.embedding VECTOR(N)`가 이미 있고,
pg_trgm은 인덱스만 추가하면 된다(`GIN(... gin_trgm_ops)`). 컬럼 변경이 필요 없다.
`VECTOR(1536)`은 예시값 그대로 두어야 한다 — 이번 PoC 모델은 384차원이지만
**production 모델이 확정되지 않았으므로 지금 차원을 바꾸면 안 된다.**

`pg_trgm` extension은 스키마 문서 §3에 이미 주석으로 존재한다(활성화는 PoC 후 결정).

---

## 11. Remaining Decisions

이번 실험으로 결정하면 **안 되는** 사항.

```text
Production embedding model 및 VECTOR dimension
    - 이번 모델은 PoC 전용 1개 선택이며 후보 비교를 하지 않았다
    - 외부 embedding API 사용 가능 여부는 보안정책 미확정 (기능명세 §1.1)

한국어 lexical search engine 최종 확정
    - PGroonga 미검증

최종 chunking strategy / table-aware chunking
    - 이번 chunking은 방식 비교용 baseline

실제 한국어 업무용어 대응
RRF 구성 요소 가중치 도입 여부
HNSW 적용 시점
RAG Top-K
refusal 판정 기준
```

---

## 12. 성능 참고값

| Method | p50 | p95 | mean |
| --- | --: | --: | --: |
| `fts_simple` | 0.2 ms | 0.4 ms | 0.3 ms |
| `fts_simple_or` | 0.4 ms | 0.7 ms | 0.4 ms |
| `trigram` | 1.2 ms | 1.7 ms | 1.2 ms |
| `vector` | 27.9 ms | 80.2 ms | 39.2 ms |
| `rrf_fts_vector` | 27.2 ms | 49.7 ms | 29.2 ms |

> **production 예상값으로 일반화하지 않는다.**
> 문서 20건 / chunk 108개이며 인덱스 없이 exact scan이다.
> vector latency의 대부분은 검색이 아니라 **질의 임베딩 생성(CPU)** 이다 —
> 실제 규모에서는 corpus 크기보다 임베딩 지연이 지배적일 수 있다.

---

## 13. Determinism

동일 설정에서 재실행 시 결과가 같은지 확인했다.

```text
2회 실행 (PYTHONHASHSEED 다름)
ranking + score digest 완전 일치
metrics.json 완전 일치
```

임베딩 점수까지 포함해 bit 단위로 동일했다. 평가가 재현 가능하다.

---

## 14. 산출물

```text
poc/search/                          코드 + fixtures
artifacts/search-poc/metrics.json               전체 지표
artifacts/search-poc/metrics_by_category.csv    category × method
artifacts/search-poc/query_results.jsonl        질의별 top-10 전체
tests/test_search_poc.py                        44 tests (DB·모델 불필요)
```

### 환경

```text
PostgreSQL 16.2 (pgserver, pip 설치형 self-contained)
pgvector 0.6.2  (번들)
pg_trgm 1.6     (PostgreSQL 16.2 contrib 소스에서 빌드 — pgserver 미포함)
Python 3.10.12 · sentence-transformers 6.0.1 · torch 2.14.0+cpu
```

`pg_trgm` 빌드 절차는 `poc/search/README.md`에 있다.
production Docker 구조는 만들지 않았다(요청 §14).
