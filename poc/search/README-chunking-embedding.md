# Chunking + Embedding PoC

결과·추천·한계: [보고서](../../docs/poc/chunking-embedding-poc-report.md).
기존 synthetic 문서 20건, 질의 30건을 사용한다. 실제 사내 corpus가 아니다.
기능명세/DB v2.3 및 원본 문서/질의 fixture는 수정하지 않는다.

## 실행 환경

Python 3.10+, Linux CPU. 측정은 CPU float32, torch intra-op 2 threads,
inter-op 1 thread, passage batch 8, query batch 1로 실행했다.
GPU나 외부 embedding API는 사용하지 않는다.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu 'torch==2.14.0+cpu'
.venv/bin/pip install -r poc/search/requirements-chunking.txt
```

기존 `pgserver`의 PostgreSQL에는 `vector`, `pg_trgm` extension이 모두 필요하다.
현재 저장소의 실행 환경에는 둘 다 설치되어 있다. 새 환경의 pg_trgm 빌드는
[기존 Search PoC README](README.md#실행)를 따른다. Extension 설치 전에는 실행이 실패하며
lexical을 모사하거나 RRF 결과를 생략해 성공으로 처리하지 않는다.

모델 다운로드는 실행과 분리한다. 아래 두 revision만 사용하며 가중치는 약 1.6 GB다.
다운로드/캐시는 git 외부의 `/tmp`에 둔다. 다른 cache 경로는 `--cache-dir`로 지정할 수 있다.

```bash
.venv/bin/hf download intfloat/multilingual-e5-small \
  --revision 614241f622f53c4eeff9890bdc4f31cfecc418b3 \
  --cache-dir /tmp/chunking-embedding-hf/hub \
  --include '*.json' '*.safetensors' 'sentencepiece.bpe.model' 'README.md' \
  --exclude 'onnx/*' 'openvino/*'
.venv/bin/hf download intfloat/multilingual-e5-base \
  --revision d128750597153bb5987e10b1c3493a34e5a4502a \
  --cache-dir /tmp/chunking-embedding-hf/hub \
  --include '*.json' '*.safetensors' 'sentencepiece.bpe.model' 'README.md' \
  --exclude 'onnx/*' 'openvino/*'
HF_HUB_OFFLINE=1 .venv/bin/python poc/search/chunking_evaluate.py
```

네트워크를 차단한 모델 worker가 하나씩 실행되고, 임시 PostgreSQL에서 exact cosine 검색을
평가한다. pgserver의 로컬 socket 및 runtime lock 디렉터리에 접근할 수 있어야 한다.
모델 로딩 시간에는 다운로드와 Python/library import가 포함되지 않는다. 한 번 warm-up 후
전체 corpus를 설정당 한 번 임베딩한다. 질의 30개는 각각 단건으로 3회 측정한다.
corpus 시간에는 입력 길이 검사/토큰화/배치 encoding/정규화/출력 검증이 포함된다.
raw RSS는 Linux 프로세스 전체 최대값이며 모델의 독립 RAM 요구량이 아니다.

## 고정 실험 계약

| ID | Chunking | 토큰 상한 | 모델 |
|---|---|---:|---|
| C1 | Fixed | 64 | e5-small |
| C2 | Paragraph | 64 | e5-small |
| C3 | Paragraph + Table | 64 | e5-small |
| C4 | Fixed | 64 | e5-base |
| C5 | Paragraph | 64 | e5-base |
| C6 | Paragraph + Table | 64 | e5-base |
| C7 | Fixed | 128 | e5-small |
| C8 | Paragraph | 128 | e5-small |
| C9 | Paragraph + Table | 128 | e5-small |

문서가 원래 58~90토큰이므로 크기만 64/128로 축소했다. 문서를 길게 늘리지 않았다.
overlap=0. 토큰 수는 model tokenizer의 special-token 제외 길이다. 실제 encoding 시
`passage: ` / `query: ` + special tokens를 포함해 512 이내인지 별도로 검사한다.
자동 truncation은 금지한다. 모델별 실제 chunk text/count/anchor가 동일함을 검증한다.

`chunking_layout.json`은 원문의 3개 금액 목록을 표로 표현하는 **synthetic 구조 주석**이다.
원문 substring 매칭이 실패하면 중단한다. 모든 청커에 같은 구조와 같은 표 직렬화 text를
제공한다. title + department + year는 첫 블록에 한 번만 들어간다. 원본 Search PoC처럼
제목 독립 청크를 강제하지 않는다. lexical은 원래의 `Document.searchable_content`를 유지한다.

본문의 기존 문장 경계를 가상 문단으로 사용한다. `paragraph_start/end`는 이 synthetic
블록의 inclusive 0-based 인덱스이며 실제 HWP의 인덱스로 주장하지 않는다.
일반 paragraph 알고리즘은 실제 구조를 전달받는 `Block` 입력에도 동작한다.
Parser 코드를 재작성하지 않았다. 표 헤더 의미 추정/병합 셀 처리/ingestion 연결은 범위 밖이다.

Paragraph는 블록 경계를 유지하고 상한 안에서 누적한다. 단일 블록이 상한보다 긴 경우만
분할한다. Table은 본문과 분리하고, 긴 표는 헤더를 반복한 row group으로 만든다.
헤더+한 행도 상한을 넘으면 명시적으로 실패한다. 셀을 버리거나 조용히 자르지 않는다.

기존 `evaluate.py`, `VectorSearch`, `TrigramSearch`, `HybridSearch`, `db.load_corpus`를
재사용한다. DB 헬퍼에만 dimension 인자와 임시 서버 종료 옵션을 추가했고 기존 기본값은 유지했다.
문서 score = 전체 청크의 최대 cosine, 문서 ID 중복 제거 후 top10. HNSW는 사용하지 않는다.
R@k는 정답 문서 집합에 대한 recall, MRR은 top10 내 첫 relevant, nDCG@5는
primary gain 2 / other relevant gain 1이다. 기존 `P@1` 표기는 **primary_at_1**을 뜻한다.
일반 precision@1 정의와 다르다. no_answer 2건은 질의 출력에 유지하고 recall을 null로 기록한다.

Vector 품질 우승은 primary_at_1 → R@5 → nDCG@5 → MRR 순으로 선택하며, 동점은 작은
dimension → paragraph/fixed/table 순의 단순성 → 작은 size로 결정한다. 이 선택과
보고서의 QUALITY + OPERABILITY 기반 구현 기본값 추천은 목적이 다르다.
우승 설정 한 개에만 pg_trgm floor 0.20 + RRF k=60, candidate_k=10을 실행한다.
질의별 튜닝, FTS 재평가, reranker, RAG 생성은 없다.

## 결과와 검증

`artifacts/chunking-embedding-poc/`:

- `configurations.json`: matrix, 완료 상태, 모델 revision, 입력/코드 SHA256, 패키지·CPU·DB 정보
- `metrics.csv`, `category_metrics.csv`, `table_metrics.csv`: 전체/12 category/기존 표 질의 2개
- `chunk_statistics.csv`, `chunks.jsonl`: 청크 통계와 재검토할 원문/anchor
- `latency.csv`, `model_measurements.json`: 모델 비용과 90개 원시 query timing/model
- `query_results.jsonl`: 모든 질의의 rank, relevance, score, 지표 (30 × 10 = 300행)
- `chunk_retrieval.jsonl`: 문서 중복 제거 전 top5 청크 점유 (30 × 9 = 270행)

`metrics.csv`의 retrieval latency는 **미리 계산한 query vector로 DB 검색하는 시간**이다.
실시간 end-to-end 지연이나 모델 지연으로 해석하지 않는다. query embedding은 `latency.csv`를 본다.
동일 입력/버전은 순위 재현 대상이며 CPU 시간·RSS는 시스템 부하에 따라 달라진다.
embedding `.npy`는 임시 디렉터리에서만 사용하고 종료 시 제거한다.
실패 시 `configurations.json.status`는 COMPLETE가 되지 않는다. 이전 출력이 있더라도
새 RUNNING 상태의 결과를 완료된 실험으로 해석하지 않는다.

```bash
.venv/bin/pytest tests/test_chunking_embedding.py tests/test_search_poc.py -q
.venv/bin/pytest -q
```

테스트용 긴 표/Unicode 문자열은 알고리즘 검증 전용이며 retrieval dataset에 추가하지 않는다.
실제 tokenizer 회귀 검사는 로컬 cache가 있을 때만 실행하며 다운로드하지 않는다.
사내 corpus 재평가 시 구조 주석도 함께 교체한다.

```bash
HF_HUB_OFFLINE=1 .venv/bin/python poc/search/chunking_evaluate.py \
  --documents /internal/documents.jsonl --queries /internal/queries.jsonl \
  --layout /internal/layout.json --output /internal/results
```

실제 사내 데이터와 원시 결과를 저장소에 넣지 않는다. corpus 교체 시 짧은 synthetic용
64/128을 회사 문서의 최적값으로 전제하지 않는다.
