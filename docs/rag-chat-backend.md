# RAG Backend + Chat API v1

기준: [API Contract §9](api-contract-v1.md), [DB v2.5](database-schema-v2.5.md),
[기능명세 v2.4](functional-spec-v2.4.md), [Design Freeze](design-freeze-v1.md).
Frozen 문서·migration·Frontend는 변경하지 않는다.

## RAG flow / ACL

```text
서버 인증 → 본인 세션 확인
→ SearchService.search(mode=semantic, page=1, size=내부 제한)
→ 기존 ACL + current READY eligibility → 문서별 best chunk ranking
→ 같은 eligibility로 matched chunk 원문 재조회
→ bounded context → provider → structured output / citation 검증
→ ACL 재검사 → user + assistant + sources 원자 저장
```

검색 랭킹·embedding loader를 새로 구현하지 않았다. 기존 `SearchService`,
`LocalQueryEmbedder` → pinned local E5, `SearchRepository`를 사용한다.
Search / Document / Chat의 READ 판정은 동일 `READ_ACL_PREDICATE`를 참조한다.
기존 검색의 document aggregation과 필터 의미는 유지한다.

검색 직후 권한이 사라지거나 current가 변경되면 context 재조회에서 제외한다.
원문은 **실제 matched chunk**이며 200자 UI snippet 또는 문서 전체 본문을 사용하지 않는다.
답변 저장 직전 모든 전달 context의 현재 ACL을 재검사한다. 하나라도 접근 불가하면
생성 결과를 폐기하고 안전한 거절만 저장한다. 모델 추론 동안 DB 연결·transaction을 잡지 않는다.

## Provider abstraction / configuration

`rag.provider.LLMProvider.generate(GenerationRequest) -> object`를 주입한다.
기본값 `UnconfiguredProvider`는 근거가 있을 때 `500 INTERNAL_ERROR`로 닫힌 상태로 실패한다.
근거가 없는 경우 provider를 호출하지 않고 거절을 정상 저장한다.
Production 구현은 아래 [Anthropic Claude provider](#anthropic-claude-provider-production-llm)이며
`LLM_PROVIDER`로 명시 선택해야만 활성화된다. 다른 hosted inference·provider URL·모델 다운로드는 없다.

테스트나 다른 승인 구현으로 교체할 때는 application composition에서 의존성을 덮어쓴다.

```python
from api.app import create_app
from api.dependencies import get_llm_provider

app = create_app()
# approved_provider는 회사 정책에 따라 별도로 검토·구성한 LLMProvider 구현.
app.dependency_overrides[get_llm_provider] = lambda: approved_provider
```

실제 provider는 instruction/question/context 구분, structured output 형식,
자체 inference timeout 및 회사 데이터 정책을 준수해야 한다.
Deterministic provider는 `tests/test_chat_http.py`에만 존재한다.

| 환경변수 | 기본값 | 의미 |
| --- | --- | --- |
| `RAG_RETRIEVAL_LIMIT` | 5 | 기존 검색에서 가져올 상위 문서 수(1~100) |
| `RAG_MAX_CONTEXT_CHUNKS` | 5 | 전달할 최대 chunk 수(1~100) |
| `RAG_MAX_CONTEXT_CHARS` | 12000 | metadata·JSON escaping 포함 context 문자 예산 |

전체 chunk 단위로 선택하며 공백 본문·예산 초과 chunk는 생략한다. 문장 중간을 잘라
근거로 사용하지 않는다. 이 값은 **implementation safety default**이며 production optimum이나
refusal threshold가 아니다. 모델 답변은 내부 8000자, citation 배열은 최대 100개로 제한한다.
Public API에 `top_k`, score 또는 튜닝 파라미터를 추가하지 않았다.

## Anthropic Claude provider (production LLM)

`LLM_PROVIDER=anthropic`일 때만 `rag.providers.anthropic_claude.AnthropicClaudeProvider`를 주입한다.
`ANTHROPIC_API_KEY`가 환경에 있어도 selector가 없으면 외부 호출이 없는 기본값을 유지한다.
공식 SDK(`anthropic`)의 Messages API만 사용하며 REST client를 직접 구현하지 않았다.

```bash
pip install -e '.[llm]'

export LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=...        # backend 전용. frontend/VITE_*/git에 두지 않는다
export ANTHROPIC_MODEL=claude-opus-5
```

| 환경변수 | 기본값 | 의미 |
| --- | --- | --- |
| `LLM_PROVIDER` | (없음) | `anthropic`일 때만 Claude 호출. 그 외에는 `UnconfiguredProvider` |
| `ANTHROPIC_API_KEY` | (없음) | 없으면 fail closed. 로그·응답·오류 메시지에 값을 넣지 않는다 |
| `ANTHROPIC_MODEL` | `claude-opus-5` | 모델 ID는 이 한 곳에서만 결정한다 |
| `ANTHROPIC_MAX_TOKENS` | 16000 | 상한. adaptive thinking 토큰을 포함하므로 낮추면 JSON이 잘린다 |
| `ANTHROPIC_TIMEOUT_SECONDS` | 60 | 최악 대기 = timeout x (max_retries + 1) |
| `ANTHROPIC_MAX_RETRIES` | 1 | SDK 자체 backoff만 사용. 별도 retry 계층을 만들지 않았다 |
| `ANTHROPIC_EFFORT` | (미전송) | 모델이 지원할 때만 설정. 미설정 시 API 기본값 |

Adapter는 `src/rag/providers/`에 둔다. `src/rag/*.py`는 vendor SDK를 import하지 않으며
`tests/test_rag.py`와 `tests/test_anthropic_provider.py`가 그 경계를 검사한다.
`RagService`는 Anthropic SDK를 직접 import하지 않는다.

Provider 역할은 **호출 → 파싱 → 내부 contract 반환**까지다. Messages API의 `system`에는
system instruction만, user turn에는 document context JSON과 질문을 **서로 다른 content block**으로
전달한다. 문서 텍스트를 instruction에 문자열로 합치지 않는다. 응답은 `output_config.format`
json_schema로 `{answerable, answer, citation_chunk_ids}` 형태를 요청하지만, citation allow-list
검증과 `refused` 결정은 그대로 `rag.validation` / `RagService`가 수행한다.
Malformed·truncated 응답과 safety `stop_reason=refusal`은 예외가 아니라 서버 거절로 수렴한다.
Client는 provider lifecycle 동안 재사용하며 request마다 만들지 않는다.

Provider 오류는 내부 예외로 정규화한다. upstream message·response body·API key·prompt를
전파하지 않는다. 429는 계약에 이미 있는 `RATE_LIMITED`(429)로, 인증 실패·timeout·network·5xx·
model unavailable은 기존 `INTERNAL_ERROR`(500)로 매핑한다. 새 error code를 만들지 않았다.
로그에는 provider·model·latency·stop_reason·input/output token 수만 남기고 질문·context·답변
전문과 key를 남기지 않는다.

## Prompt / grounding / refusal

`GenerationRequest`는 system instruction, 사용자 질문, JSON document context를 별도 필드로 유지한다.
문서의 제목·본문 모두 untrusted data다. 문서 내부의 명령 실행, 다른 문서·도구 요청,
자체 지식 보충을 금지하고 근거가 부족하면 답변 불가를 반환하도록 지시한다.
질문이나 문서 내용으로 system instruction을 구성하지 않는다.

Provider result는 다음 strict schema를 따른다. 문자열에서 의미를 추측하거나 JSON 일부를 복구하지 않는다.

```json
{"answerable": true, "answer": "근거에 따른 답변", "citation_chunk_ids": ["전달된 chunk UUID"]}
```

서버가 전달 chunk ID allow-list와 대조하고, sources의 document/revision/title/anchor는
DB provenance에서 재구성한다. 모델이 임의 metadata를 추가해도 schema 검증 실패다.
존재하는 chunk라도 **이번 context에 전달하지 않았다면 인용 불가**다.
가짜 citation이 하나라도 섞이면 전체 생성을 거절한다. 잘못된 ID만 제거한 뒤
그 ID에 의존했을 수 있는 답변을 그대로 보존하지 않는다.

거절 조건: usable context 없음, `answerable=false`, 정상 답변의 citation 없음,
공백 답변, malformed output, allow-list 밖 citation. 기본 문구는 계약의
**“관련 문서에서 확인할 수 없습니다.”**다. 정상적으로 구조화된 answerable=false 결과에는
유효 출처를 함께 저장할 수 있다. `refused`는 문자열·출처 수에서 복원하지 않고 직접 저장한다.
Vector score는 랭킹에만 사용하며 답변 신뢰도나 refusal 확률로 사용하지 않는다.

Anchor는 HWP/HWPX/DOCX의 실제 paragraph, PDF의 저장된 검증 page, 그 외 `none`이다.
HWP/HWPX의 page를 추정하지 않는다.

## Chat persistence / historical ACL

기존 `chat_sessions`, `chat_messages`, `chat_message_sources`만 사용한다.
Provider failure에는 메시지를 저장하지 않는다. 생성/검증 완료 후 한 짧은 transaction에서
user(`refused=NULL`), assistant(`refused=TRUE/FALSE`), 모든 source를 함께 저장한다.
Source insert 실패 시 user까지 rollback한다. 세션 행을 저장 시점에만 잠가 동시 요청의
완성된 메시지 쌍이 섞이지 않게 하며, source에는 `chunk_id`만 저장한다.
`rrf_score`는 NULL이고 revision ID를 중복 저장하지 않는다.

과거 sources는 `stored chunk → 당시 revision → document`로 복원하며 current 변경과 무관하다.
세션 읽기는 일관된 DB snapshot에서 현재 ACL과 soft-delete 상태를 다시 검사한다.
접근 불가 source는 `accessible=false`와 감사용 document/revision/chunk ID만 반환한다.
title, file_type, section_title, anchor를 생략한다. 하나라도 접근 불가하면
`has_inaccessible_sources=true`, **`content_hidden=true`, `content=null`**로 답변 전체를 숨긴다.
DB의 원래 답변과 `refused` 값·source row는 유지한다.
사용자 메시지와 명시적으로 입력한 세션 제목은 사용자 소유 기록으로 유지한다.

## Routes / errors / logging

| Method | Route | 성공 | Pagination |
| --- | --- | --- | --- |
| POST | `/api/v1/chat/sessions` | 201 | 선택 title, 생략 시 null |
| GET | `/api/v1/chat/sessions` | 200 | 본인만, page=1/size=20, updated_at DESC/id ASC |
| GET | `/api/v1/chat/sessions/{session_id}` | 200 | 메시지 page=1/size=50, created_at ASC/id ASC |
| POST | `/api/v1/chat/sessions/{session_id}/messages` | 201 | message 1~4000자 |

페이지 크기는 최대 100이며 범위 밖 페이지에도 total을 유지한다.
Request body의 추가 필드와 알 수 없는 query parameter를 거절한다.
API method/path 집합은 기존 6개 + Chat 4개 = **10개**다(고유 URL path 수와 구분).
다른 사용자·없는·잘못된 UUID 세션은 모두 `404 CHAT_SESSION_NOT_FOUND`.
인증 없음/비활성 사용자는 401, 4000자 초과는 `422 CHAT_MESSAGE_TOO_LONG`,
나머지 입력 검증은 422, provider rate limit은 계약의 기존 `429 RATE_LIMITED`,
그 밖의 provider failure는 500이다. 공통 error envelope / request ID를 사용한다.
CORS의 기존 origin allow-list는 유지하고 허용 method에 POST만 추가했다.

Router는 검증·인증 의존성·service 호출·응답 매핑만 한다.
Chat 오류 로그에 exception 메시지·SQL 파라미터·provider secret을 포함하지 않는다.
RAG 완료 로그는 request/session/message ID, context 개수, refused, 지연, provider 식별자만 기록한다.
질문·prompt·chunk text·답변 전문을 info log에 기록하지 않는다.

## Validation

```bash
# 저장소 루트: 기존 가상환경 및 PostgreSQL 테스트 구성 사용
OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
EMBEDDING_CACHE_DIR=/tmp/chunking-embedding-hf/hub \
.venv/bin/python -m pytest -q

cd frontend
npm test
npm run build
```

기본 pytest 실행은 Claude API를 호출하지 않는다. Anthropic adapter test는 SDK client를 fake로 두고
정상 structured response, `answerable=false`, invalid JSON/schema, timeout, 429, 인증 실패, 5xx,
network error, model unavailable, prompt 경계, secret 미노출을 검증한다.

실제 API를 쓰는 smoke test는 opt-in이며 비민감 synthetic 문서만 사용한다.

```bash
RUN_ANTHROPIC_INTEGRATION=1 LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=... \
.venv/bin/python -m pytest tests/test_anthropic_smoke.py -q
```

모델 cache 경로는 환경에 맞게 설정한다. 테스트는 새 모델을 다운로드하지 않는다.
Chat integration은 실제 PostgreSQL/migration, 기존 semantic 검색, deterministic test provider로
ACL 누출·current/historical 구분·refusal·출처 위조·prompt 경계·transaction·동시 저장·네트워크 부재를 검증한다.
OpenAPI 테스트는 10개 method/path, 입력 identity 부재, 출력 schema/anchor, 공통 오류를 검사한다.

## Architecture Notes / known limitations

1. **Claude API integration은 구현됐지만, 사내 문서 production 사용은 조직 승인이 필요하다.**
   Claude API는 prompt와 document context를 외부 네트워크로 전송한다. 코드 준비 상태와
   외부 LLM 사용 정책 승인은 별개이며, 승인 여부를 임의로 가정하지 않는다.
   승인 전에는 `LLM_PROVIDER`를 설정하지 않는 것이 기본값이고 그 상태에서 외부 호출은 없다.
2. `chat_messages`의 `model`, `input_tokens`, `output_tokens` 컬럼은 schema에 이미 있으나 아직
   채우지 않는다. 채우려면 provider가 usage를 함께 반환해야 하고 이는 `LLMProvider` interface
   변경이므로 이번 범위 밖이다. usage는 로그로만 남긴다. Migration은 필요 없다.
3. Anthropic `fallbacks`(정책 거절 시 다른 모델로 자동 재실행)는 **의도적으로 켜지 않았다.**
   어떤 모델이 사내 문서를 처리하는지가 승인 대상이므로 자동 모델 전환은 승인 범위를 넓힌다.
   safety `stop_reason=refusal`은 안전한 거절로 처리한다.
4. Claude의 grounding·refusal 품질과 prompt injection 저항성은 smoke 수준에서만 확인했다.
   smoke test 통과가 완전한 보안 보장을 뜻하지 않는다.
5. Citation 검증은 **출처 provenance 검증**이다. 유효한 chunk를 인용했다는 사실만으로 답변의 모든
   문장이 해당 chunk에서 논리적으로 도출됨을 증명하지 않는다. Fake/adapter 테스트가 보장하는 것은
   지시·데이터 분리 경계와 allow-list 강제이지 모델 답변의 논리적 근거성이 아니다.
6. 첫 버전은 질문마다 독립 retrieval을 수행한다. 과거 대화 본문을 모델 context에 자동 재투입하지 않는다.
   문서별 best chunk 하나와 문자 예산의 한계로 여러 문단에 흩어진 근거를 놓칠 수 있다.
   Top-K/context 정책의 최적값 및 semantic relevance/refusal threshold는 계속 OPEN이다.
7. 계약 §9의 `refused` 컬럼 미존재/OI-2 설명은 DB v2.5 및 migration에서 이미 해소되었다.
   기존 컬럼과 role CHECK를 사용하며 frozen 계약을 수정하지 않았다.
8. Chat의 4000자 입력과 E5의 token 한도는 다른 단위다. 기존 query embedding wrapper의
   모델 길이 처리 방식을 재사용하므로 긴 질문의 검색 품질은 별도 검증이 필요하다.
9. 인증/ACL은 각 DB 검사 시점의 상태를 따른다. 모델 호출과 DB 권한 변경을 하나의 장기 transaction으로
   묶지 않는다. 현재 SSO는 기존과 동일하게 미구현이며 production 인증은 401로 닫혀 있다.

다음 단계: **React Chat UI 구현**.
