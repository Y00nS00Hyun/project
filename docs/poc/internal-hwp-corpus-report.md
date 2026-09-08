# 사내 HWP/HWPX Corpus 재측정 보고서

* 작성일: 2026-09-07
* 대상 질문: **현재 구현한 HWP/HWPX Parser를 실제 사내 문서 ingestion에 사용해도 되는가?**
* 선행 문서: `docs/poc/hwp-poc-report.md`, `docs/functional-spec-v2.3.md`, `docs/database-schema-v2.3.md`
* 범위: 측정 및 판단만. production 서비스는 구현하지 않았다.

---

## 0. 이 보고서의 현재 상태

> ### ⛔ 측정 미실행 — 사내 corpus를 확보하지 못했다.
>
> 이 작업이 수행된 환경에는 **사내 문서가 한 건도 존재하지 않는다.**
> 공유폴더 마운트(CIFS/NFS/SMB) 없음, `/mnt`·`/media`·`/srv`·`/internal` 없음,
> 파일시스템 전체에서 발견된 `.hwp`/`.hwpx`는 이 저장소의 pytest 임시 파일뿐이었다.
>
> 따라서 **§3~§13의 수치는 비어 있다.** 채워 넣지 않았다.
>
> 이는 작업 요청 §3의 역할 분담과 일치한다.
>
> ```text
> Codex     → 실행 코드 및 분석 스크립트 작성   ← 이번에 완료한 부분
> 사내 VM   → 실제 사내 문서 parsing 실행       ← 아직 실행되지 않음
> ```
>
> 이번 작업의 산출물은 **사내 VM에서 실행할 측정 도구**와
> **그 도구가 정상 동작함을 증명하는 검증 결과**(§16, 공개 corpus 사용)다.
>
> 실행 방법은 §15에 있다. 실행 후 `artifacts/internal-hwp-poc/summary.json`의
> 값으로 §2~§13을 채우면 이 보고서가 완성된다.

### 판단

```text
INSUFFICIENT DATA
```

사내 문서 0건으로는 production 적합성을 판단할 수 없다.
**임의의 임계치를 만들어 PASS로 처리하지 않았다.**

선행 PoC 결론(`PASS WITH LIMITATIONS`)은 공개 third-party corpus 98건 기준이며,
그 보고서에도 사내 호환성 미검증(KL-4)이 명시되어 있다. 이 상태는 변하지 않았다.

---

## 1. Executive Summary

| 항목 | 상태 |
| --- | --- |
| 사내 corpus 측정 | ❌ 미실행 — 문서 확보 불가 |
| 측정 도구 | ✅ 구현 완료 (`scripts/run_internal_corpus.py`) |
| 도구 검증 | ✅ 공개 corpus 98건으로 end-to-end 검증 (§16) |
| 익명화 / 정보유출 방지 | ✅ 구현 및 테스트 완료 (§14) |
| parser 코드 변경 | 없음 — 명확한 버그가 발견되지 않음 |
| **최종 판단** | **INSUFFICIENT DATA** |

### 이번 작업에서 실제로 확인한 것

측정은 못 했지만, 측정 방법론 자체에서 **선행 PoC의 수치 하나를 정정**했다.

선행 PoC는 메모리를 `tracemalloc`(Python 할당량)으로 측정해
`peak memory ≈ 파일 크기 × 5`로 기록했다. 이번에 프로세스 단위 실제 RSS로 다시 재면
값이 다르고, **포맷별 차이가 매우 크다**(§16.3).

```text
HWPX   파일 크기 1 MB당 marginal RSS 약 9.5–11 MB
HWP    파일 크기 1 MB당 marginal RSS 약 0.5 MB
```

worker memory limit은 `× 5` 같은 단일 상수가 아니라 **포맷별로** 산정해야 한다.
다만 이 관측 자체도 1 MB 이상 표본이 3건뿐이므로 production 값의 근거로는 부족하다.

---

## 2. Corpus Overview

> 측정 미실행. `summary.json`의 `corpus` 블록으로 채운다.

| 항목 | 값 |
| --- | --- |
| 총 문서 수 | — |
| HWP | — |
| HWPX | — |
| 크기 분포 (< 1MB / 1–5 / 5–20 / 20–50 / > 50MB) | — |

문서명·경로는 기록하지 않는다(§14).

---

## 3. Parsing Success Rate

> 측정 미실행. HWP와 HWPX를 **반드시 분리**하여 기록한다.

| Format | 문서 수 | TEXT_EXTRACTED | 성공률 |
| --- | --: | --: | --: |
| HWP | — | — | — |
| HWPX | — | — | — |

전체 성공률 하나만 기록하지 않는다.

---

## 4. Result Code Distribution

> 측정 미실행. `summary.json`의 `result_matrix`로 채운다.

| Result | HWP | HWPX | Total | Rate |
| --- | --: | --: | --: | --: |
| TEXT_EXTRACTED | — | — | — | — |
| EMPTY_DOCUMENT | — | — | — | — |
| OCR_REQUIRED | — | — | — | — |
| ENCRYPTED | — | — | — | — |
| CORRUPT | — | — | — | — |
| UNSUPPORTED_FORMAT | — | — | — | — |
| PARSE_FAILED | — | — | — | — |

---

## 5. HWP Production Readiness

> 측정 미실행.

선행 PoC에서 **본문을 확보한 HWP 표본이 6건뿐**이었고 그중 표를 가진 문서는 1건이었다.
사내 corpus에서도 HWP 표본이 부족하면 다음과 같이 명시하고 적합성을 확정하지 않는다.

```text
INSUFFICIENT HWP SAMPLE
```

판단에 필요한 최소 조건(측정 후 평가):

```text
- 본문 확보 HWP 문서 수가 통계적으로 의미 있는 수준인가
- 표 포함 HWP 문서에서 구조 보존이 확인되는가
- ENCRYPTED(배포용) 비율이 운영 프로세스를 요구하는 수준인가
```

---

## 6. HWPX Production Readiness

> 측정 미실행.

선행 PoC에서 HWPX는 60건 전건 안정적이었고 독립 구현체 대비 토큰 결손이 0.02%였다.
사내 corpus에서 이 수준이 재현되는지 확인한다.

---

## 7. Table Handling

> 측정 미실행.

도구는 표 포함 문서마다 **파일이 스스로 선언한 구조와 복원 결과를 대조**하여
자동 판정을 남긴다.

| 판정 | 의미 |
| --- | --- |
| `PASS` | 선언된 행·열 수와 복원 결과가 일치하고 경고 없음 |
| `PASS_WITH_WARNING` | 구조는 복원되었으나 병합 셀·중첩 표·주소 판독 실패 등이 있음 |
| `FAIL` | 복원 결과가 파일 선언과 불일치하거나 grid가 비어 있음 |

자동 판정은 **사람의 확인을 대체하지 않는다.** 도구는 사람이 확인할 순서를 정한
worklist를 `artifacts/internal-hwp-poc/table_review.csv`에 함께 출력한다
(FAIL → PASS_WITH_WARNING → PASS 순, 셀 수 내림차순).

요청 §10에 따라 **최소 10건 이상**을 사내 VM에서 육안 확인하고
`human_verdict` 열에 `PASS` / `PASS_WITH_WARNING` / `FAIL`을 기록한다.
확인 항목: 셀 내용 누락, 행 순서, 열 순서, 병합 셀 경고, 본문과 표의 등장 순서.

**문서 내용은 보고서에 옮기지 않는다.**

---

## 8. Citation Anchor

> 측정 미실행.

| 지표 | 목표 | 결과 |
| --- | --- | --- |
| Paragraph Anchor Availability Rate (TEXT_EXTRACTED 기준) | 100% | — |
| section_title 확보 문서 비율 | 목표 없음 (참고값) | — |

`paragraph_index`는 0..n-1 연속성까지 검사한다. 100% 미달 시 원인을 조사한다.

`section_title`은 필수 조건이 아니며(기능명세 v2.3 §14.3),
확보율을 높이기 위한 heuristic을 새로 추가하지 않는다.

---

## 9. Determinism

> 측정 미실행.

포맷별 대표 표본(기본 5건씩: 최대 크기 / 표 최다 / 문단 최다 우선)을 각 3회 파싱하여
`canonical_hash`, `normalized_text_hash`, `paragraph_count`, `table_count`를 비교한다.

불일치가 있으면 **production blocker 후보**로 분류한다.

---

## 10. Performance

> 측정 미실행. `performance.csv`로 채운다.

크기 bucket × 포맷별로 문서 수, 평균 / P50 / P95 / 최대 parse time을 기록한다.

---

## 11. Memory

> 측정 미실행. `performance.csv` 및 `summary.json`의 `peak_rss_by_format`으로 채운다.

측정 방식이 선행 PoC와 다르다는 점에 주의한다.

| 지표 | 의미 |
| --- | --- |
| `peak_rss_mb` | worker 프로세스 전체 peak RSS. **worker memory limit의 근거** |
| `rss_over_baseline_mb` | 위 값에서 인터프리터 baseline을 뺀 값. 문서에 귀속되는 부분 |
| `peak_memory_mb` | `tracemalloc` 기준 Python 할당량. 선행 PoC와 비교하기 위한 값 |
| `rss_per_mb_ratio` | `rss_over_baseline / 파일크기`. **1 MB 이상 문서만** 집계 |

1 MB 미만 문서를 비율 계산에서 제외하는 이유: peak RSS가 문서가 아니라
인터프리터 baseline(약 20 MB)에 지배되어 비율이 무의미해진다.
제외하지 않으면 0.01 MB 문서에서 `2000×` 같은 값이 나온다.

---

## 12. Operational Recommendations

> 측정 미실행 — **현재 근거로는 어떤 값도 확정하지 않는다.**

```text
Worker memory limit     결정 불가
동시 parsing worker 수   결정 불가
최대 허용 파일 크기       결정 불가
timeout                 결정 불가
```

측정 후 다음 근거로 산정한다.

| 값 | 산정 근거 |
| --- | --- |
| Worker memory limit | 포맷별 `peak_rss_mb`의 최대값 + 여유. HWPX와 HWP를 분리 |
| 최대 허용 파일 크기 | 최대 크기 bucket의 `peak_rss_mb`가 limit을 넘기 시작하는 지점 |
| timeout | `parse_time_ms` P95의 배수 |
| 동시 worker 수 | (가용 메모리 − 여유) / worker memory limit |

표본이 부족한 구간은 그대로 남긴다. 예:

```text
Production 값을 아직 확정하기에는 >50MB 문서 표본이 부족함.
```

---

## 13. Known Limitations

> 사내 corpus 측정 후 이 절에 실제 발견 사항을 기록한다.

현재 시점에서 이미 알려진 한계(선행 PoC `docs/poc/hwp-poc-report.md` §10 및
기능명세 v2.3 §28.2)는 그대로 유효하다.

```text
KL-1  HWP/HWPX page anchor 확보 불가 (포맷의 성질)
KL-2  배포용/암호화 HWP는 본문 ingestion 불가
KL-3  section_title 확보율이 낮음
KL-4  사내 문서 호환성 미검증          ← 이번에도 해소되지 않음
KL-5  중첩 표·머리말/꼬리말·각주·수식 미수집
```

### 이번 작업에서 추가로 확인된 한계

**KL-6. 선행 PoC의 메모리 수치는 Python 할당량 기준이었다.**

`peak memory ≈ 파일 크기 × 5`는 `tracemalloc` 값이며 worker 프로세스의 실제 RSS가 아니다.
실제 RSS로 재측정하면 값이 다르고 포맷별 편차가 크다(§16.3).
worker sizing에는 RSS 기준값을 사용해야 한다.

**KL-7. 자동 표 판정은 "파일 선언과의 일치"만 검증한다.**

파일이 선언한 행·열 수와 복원 결과가 일치하는지는 자동으로 검증할 수 있지만,
셀 내용이 원문과 같은지는 자동으로 알 수 없다. 사람의 표본 확인이 필요하다(§7).

---

## 14. 정보 유출 방지 설계

실제 사내 문서를 다루므로, 측정 결과가 사내 밖으로 나가도 안전하도록 설계했다.

### 산출물에 들어가지 않는 것

```text
원본 파일명
전체 경로
문서 본문 / 발췌 / snippet
개인정보, 계약·인사 내용
문서 내부 고유 데이터
```

### 익명화

문서는 `HWP-001`, `HWPX-001` 형태의 식별자로만 기록한다.
식별자는 경로 해시 순으로 부여되므로 디렉터리 구조나 파일명 알파벳 순서를 드러내지 않으며,
같은 corpus를 다시 측정해도 동일하게 유지된다.

### 실패 사유 정규화

실패 사유는 자유 문자열 그대로 저장하지 않는다.
경로·따옴표로 묶인 이름을 제거하고 숫자를 `N`으로 치환한 뒤 80자로 자른 **버킷 라벨**만 남긴다.

```text
"cannot open /internal/share/인사/2026 급여명세.hwp"
→  "cannot open"
```

이 동작은 테스트로 고정되어 있다
(`tests/test_corpus.py::TestPrivacy`, 한글 문서명·Windows 경로·바이트 오프셋 포함).

`CorpusRecord`에 경로나 본문을 담을 수 있는 필드가 없다는 것도 테스트로 강제한다.

### 매핑 파일

발견 사항을 실제 문서까지 되짚어야 할 때만 `--mapping-file`로 명시적으로 요청한다.
이 파일에는 실제 경로가 들어가므로 **사내 VM 밖으로 내보내지 않는다.**
`.gitignore`에 `*.corpus-mapping.json`, `internal-corpus/`,
`artifacts/internal-hwp-poc/`를 추가했다.

### 외부 전송

이 도구는 네트워크를 사용하지 않는다. 외부 LLM/API로 문서를 보내지 않는다.

---

## 15. 실행 방법 (사내 VM)

```bash
# 1) 저장소를 사내 VM에 배치하고 의존성 설치
python -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 2) 도구 자체 테스트 (사내 문서 불필요)
.venv/bin/pytest tests/ -q

# 3) 사내 corpus 측정 (읽기 전용)
.venv/bin/python scripts/run_internal_corpus.py \
    --input /internal/share/sample-corpus \
    --output artifacts/internal-hwp-poc \
    --label "사내 표본 corpus 2026-09"
```

주요 옵션:

| 옵션 | 기본값 | 용도 |
| --- | --- | --- |
| `--timeout` | 300초 | 문서당 제한. 초과 시 자식 프로세스를 종료하고 `PARSE_FAILED` / `TIMEOUT` |
| `--determinism-samples` | 5 | 포맷별 반복 검증 표본 수 |
| `--determinism-runs` | 3 | 표본당 반복 파싱 횟수 |
| `--limit` | 없음 | 앞 N건만 (시험 실행용) |
| `--mapping-file` | 없음 | 익명 id → 실제 경로. **실제 경로 포함, 사내 보관** |

### corpus 구성 권고

랜덤 100건보다 유형 다양성이 중요하다(요청 §5).
HWP/HWPX, 짧은·긴·대용량 문서, 표 없음/많음/복잡, 오래된·최근 문서,
사업계획서·보고서·회의자료·공문·양식, 이미지 위주·스캔 문서,
배포용/암호화 문서, 한글+숫자·한글+영문·특수문자 문서를 포함한다.

**특히 HWP 5.x와 표 포함 HWP 문서를 의도적으로 충분히 포함할 것.**
선행 PoC에서 가장 근거가 얇았던 부분이다.

### 산출물

```text
artifacts/internal-hwp-poc/
├── summary.json        집계 전체
├── aggregate.csv       문서별 측정값 (익명)
├── performance.csv     크기 bucket × 포맷 성능·메모리
└── table_review.csv    표 문서 육안 확인 worklist
```

`summary.json`과 두 CSV는 익명화되어 있어 외부 공유가 가능하다.
`table_review.csv`도 익명 id만 포함한다.

---

## 16. 도구 검증 (공개 corpus)

> ⚠️ **이 절의 수치는 사내 문서가 아니다.**
> 선행 PoC와 동일한 공개 third-party corpus 98건이며,
> **사내 적합성의 근거로 사용할 수 없다.**
> 측정 파이프라인이 정상 동작함을 보이기 위한 검증 실행이다.

### 16.1 실행 결과

```text
문서 98건 (HWP 16, HWPX 82) / 총 16.9 MB / 소요 12.2초
문서당 1개 자식 프로세스, 인터프리터 baseline RSS 20.1 MB
```

| Result | HWP | HWPX | Total | Rate |
| --- | --: | --: | --: | --: |
| TEXT_EXTRACTED | 6 | 60 | 66 | 67.3% |
| EMPTY_DOCUMENT | 7 | 17 | 24 | 24.5% |
| OCR_REQUIRED | 1 | 5 | 6 | 6.1% |
| ENCRYPTED | 2 | 0 | 2 | 2.0% |
| CORRUPT | 0 | 0 | 0 | 0% |
| UNSUPPORTED_FORMAT | 0 | 0 | 0 | 0% |
| PARSE_FAILED | 0 | 0 | 0 | 0% |

선행 PoC의 분포(66 / 24 / 6 / 2)와 정확히 일치한다 —
새 측정 경로가 기존 결과를 재현함을 의미한다.

| Format | n | TEXT_EXTRACTED | paragraph anchor | section_title | 표 포함 문서 |
| --- | --: | --: | --: | --: | --: |
| HWP | 16 | 37.5% | 100% | 0% | 1 |
| HWPX | 82 | 73.2% | 100% | 10.0% | 29 |

표 자동 판정: HWP `PASS_WITH_WARNING` 1건,
HWPX `PASS_WITH_WARNING` 24건 / `PASS` 5건, **`FAIL` 0건.**

Determinism: 표본 10건(HWP 5 / HWPX 5) × 3회 → **10/10 안정.**

### 16.2 성능

| Format | P50 | P95 | Max |
| --- | --: | --: | --: |
| HWP | 29 ms | 37 ms | 37 ms |
| HWPX | 11 ms | 212 ms | 737 ms |

### 16.3 메모리 (선행 PoC 수치 정정)

| Format | peak RSS P50 | peak RSS P95 | peak RSS Max |
| --- | --: | --: | --: |
| HWP | 20.2 MB | 21.2 MB | 21.3 MB |
| HWPX | 20.1 MB | 37.5 MB | 88.0 MB |

파일 크기 1 MB당 marginal RSS (1 MB 이상 문서만):

| Format | 표본 수 | 평균 |
| --- | --: | --: |
| HWP | 1 | 약 0.5× |
| HWPX | 2 | 약 10.3× |

**표본이 각각 1건·2건이므로 production 산정식으로 사용할 수 없다.**
다만 포맷 간 차이가 20배 수준으로 크다는 점은 분명하며,
단일 상수(`× 5`)로 worker를 사이징하면 안 된다는 근거로는 충분하다.

### 16.4 실패 사유 버킷 및 확장자 불일치

```text
ENCRYPTED       DISTRIBUTION_DOCUMENT                                      2
OCR_REQUIRED    no extractable text (N chars) but N embedded image ...     6
EMPTY_DOCUMENT  no extractable text (N chars) and no images               24
```

확장자/컨테이너 불일치(OI-6): 공개 corpus에서는 0건.
별도 합성 케이스(HWPX를 `.hwp`로 개명)로 탐지 동작을 확인했다 →
`hwp -> zip` 1건으로 집계되고 `UNSUPPORTED_FORMAT`으로 분류됨.

### 16.5 worker 격리 확인

정상 문서·절단 파일·쓰레기 파일·확장자 오기재 파일을 섞은 배치에서
개별 실패가 배치를 중단시키지 않고 각각 분류되었다.
자식 프로세스가 신호로 종료되는 경우도 `WORKER_EXIT_<code>` 버킷으로 처리된다.

---

## 17. Architecture Impact

```text
NO ARCHITECTURE CHANGE REQUIRED
```

`docs/functional-spec-v2.3.md`와 `docs/database-schema-v2.3.md`에 수정이 필요한 사항은
현재 없다. 근거:

* v2.3의 `parse_result_code` 7개 값 도메인이 측정 도구가 산출하는 코드와 정확히 일치한다.
  (`tests/test_corpus.py::TestResultCodeTaxonomy::test_matches_the_parser_error_taxonomy`가
  parser 예외 taxonomy와의 일치를 강제한다.)
* v2.3 §8.2의 `parse_status` / `parse_result_code` 분리 규칙을 그대로 구현했다
  — 파서가 의도적으로 내린 판정은 `SUCCESS`, 원인 불명 실패만 `FAILED`.
* v2.3 §21.4가 요구한 "재측정 후 worker 정책 결정"은 아직 미결 상태로 남아 있으며,
  이는 명세의 의도대로다. 명세 변경이 아니라 값 채우기가 남은 것이다.

단, §16.3의 메모리 측정 방식 차이는 **선행 PoC 보고서의 서술**에 해당하며
v2.3 명세는 이미 `× 5`를 "관찰값이며 production 공식이 아님"으로 기술하고 있어
수정이 필요하지 않다.

사내 corpus 측정 결과에 따라 다음이 발생하면 그때 명세 변경을 검토한다.

```text
- 사내 HWP 성공률이 운영 불가 수준 → 지원 포맷 정책 재검토
- ENCRYPTED 비율이 높음            → 원본 재수급 운영 프로세스 신설 (OI-5)
- 확장자 불일치가 잦음              → File Sync 시그니처 기반 라우팅 (OI-6)
- determinism 불일치 발견           → production blocker, 재설계
```

---

## 18. Next Recommendation

```text
사내 표본 corpus 100건 이상을 확보하여 scripts/run_internal_corpus.py를 실행한다.
```

이 하나만 수행한다. 다른 기술 검증(한국어 lexical search PoC, DOCX/PDF parser PoC,
chunking 설계)이나 서비스 구현으로 넘어가지 않는다.

이유: 현재 미결 사항의 대부분이 이 측정 하나에 걸려 있다.

```text
HWP 5.x production ingestion 가능 여부       (선행 PoC L4 / KL-4)
worker memory limit / 최대 파일 크기 / 동시성 (기능명세 v2.3 §21.4)
배포용 HWP 운영 프로세스 필요 여부            (OI-5)
확장자 불일치 자동 라우팅 필요 여부           (OI-6)
OCR 기능 우선순위                            (OCR_REQUIRED 비율)
```

도구와 절차는 준비되어 있으므로, 필요한 것은 **문서 확보와 사내 VM에서의 실행**뿐이다.
