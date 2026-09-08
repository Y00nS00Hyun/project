# 한국어 검색 PoC

`simple` FTS / pg_trgm / Vector / RRF Hybrid를 **동일한 통제 데이터**에서 비교한다.

결과 해석은 `docs/poc/korean-search-poc-report.md`를 따른다.

> ⚠️ 여기 있는 corpus는 **synthetic**이다. 실제 사내 문서를 대표하지 않는다.
> 이 실험으로 확인할 수 있는 것은 검색 방식 간 **상대적 강점과 실패 유형**뿐이다.

## 구조

```text
poc/search/
├── fixtures/
│   ├── documents.jsonl     20건 (가상 조직·가상 문서)
│   └── queries.jsonl       30건 (12개 failure-mode category)
├── dataset.py              로딩 + baseline chunking
├── db.py                   PoC 전용 PostgreSQL 테이블 (production 스키마 아님)
├── lexical_search.py       simple FTS (AND/OR), pg_trgm
├── vector_search.py        embedding + pgvector
├── hybrid_search.py        RRF
├── evaluate.py             지표 계산 및 실행
└── README.md
```

## 실행

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]" pgserver "psycopg[binary]" sentence-transformers

# 전체 비교
cd poc/search && python evaluate.py --output ../../artifacts/search-poc

# 임베딩 없이 lexical만 (빠름)
python evaluate.py --no-vector --output ../../artifacts/search-poc
```

`pg_trgm`은 `pgserver` 배포판에 포함되어 있지 않아 별도 빌드가 필요하다.

```bash
D=$(python -c "import pgserver,os;print(os.path.dirname(pgserver.__file__))")/pginstall
curl -sSLO https://ftp.postgresql.org/pub/source/v16.2/postgresql-16.2.tar.bz2
tar xjf postgresql-16.2.tar.bz2 postgresql-16.2/contrib/pg_trgm
cd postgresql-16.2/contrib/pg_trgm
make install USE_PGXS=1 PG_CONFIG=$D/bin/pg_config
```

## 데이터셋 교체

평가 코드는 데이터셋에 의존하지 않는다. 사내 문서를 확보하면 파일만 바꿔 같은 비교를 다시 돌린다.

```bash
python evaluate.py \
    --documents /internal/export/documents.jsonl \
    --queries   /internal/export/queries.jsonl \
    --output    artifacts/search-poc-internal
```

## 라벨 규칙

| 필드 | 의미 |
| --- | --- |
| `relevant_documents` | 이 질문에 실제로 답하는 문서 **전체** |
| `primary_document` | 사용자가 가장 원했을 **단 하나**의 문서 |
| `category` | 검증하려는 failure mode |
| `difficulty` | 저자 주관 난이도 (분석 보조용, 지표 계산에는 미사용) |

연도가 명시되지 않아 여러 해의 문서가 모두 맞는 경우(예: `출장비`)
**최신 연도 문서를 primary로 둔다.** 사용자의 기본 의도를 현행 문서로 보기 때문이다.
`no_answer` 질의는 `relevant_documents = []`, `primary_document = null`이다.

## corpus 설계 원칙

**질의의 핵심 단어가 정답 문서에만 존재하지 않도록** 의도적으로 겹치게 만들었다.

```text
"AI 문서관리"        DOC-001~005, DOC-020        (연도·문서유형만 다름)
"예산"               DOC-002/003/013/016/018/020
"시스템 운영지침"     DOC-008(정보보안팀), DOC-009(AI플랫폼팀)
"출장비"             DOC-006(2025), DOC-007(2026)
"교육 운영계획"       DOC-013(2026), DOC-018(2025)
"120,000,000원"      DOC-003(서버 증설), DOC-016(정보보안 개선)
```

`spacing` / `typo` / `morphology` / `semantic` 질의는 정답 문서에도 문자열이
그대로 등장하지 않는다. 어휘 일치로 풀리면 안 되는 유형이기 때문이다.

## 고정 파라미터

질의별 튜닝을 하지 않는다. 모든 질의에 동일 설정을 적용한다.

| 항목 | 값 |
| --- | --- |
| FTS configuration | `simple` |
| trigram floor | `0.20` (전 질의 공통) |
| RRF `k` | `60` |
| chunking | title + 문장 단위 (baseline, 확정 아님) |
| document score | best matching chunk |
| embedding model | `intfloat/multilingual-e5-small` (**PoC 전용, production 미확정**) |

## 테스트

```bash
pytest tests/test_search_poc.py -q
```

PostgreSQL과 임베딩 모델 없이 실행된다(데이터셋 계약·RRF·지표 검증).
