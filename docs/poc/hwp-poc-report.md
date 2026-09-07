# HWP / HWPX Parser PoC 보고서

* 작성일: 2026-09-07
* 대상 질문: **HWP 5.x 및 HWPX 파일을 사내 문서 검색/RAG 시스템에서 사용할 수 있을 정도로 안정적으로 파싱할 수 있는가?**
* 범위: 파싱 검증만. 백엔드/프론트엔드/RAG/Embedding/DB migration은 구현하지 않았다.
* 참조 명세: `docs/functional_spec.MD`, `docs/database_schema.MD` (수정하지 않음)

---

## 1. Executive Summary

### 결론: **PASS WITH LIMITATIONS**

HWP 5.x와 HWPX 모두 본문·문단순서·표를 검색/RAG에 사용할 수 있는 수준으로 추출할 수 있음을 확인했다.
다만 **page anchor는 두 포맷 모두 확보 불가**이며, **배포용(distribution) HWP 문서는 원천적으로 ingestion 불가**다.
이 두 가지는 구현으로 해결할 수 있는 문제가 아니라 포맷의 성질이므로, 기능 명세의 출처 표기 설계에 반영이 필요하다.

| # | 검증 항목 | 결과 | 근거 |
|---|---|---|---|
| 1 | HWP 5.x 본문 추출 | ✅ 가능 | 공개 코퍼스 HWP 성공 파일 전건 본문 추출 |
| 2 | HWPX 본문 추출 | ✅ 가능 | 공개 코퍼스 HWPX 60/60 성공 파일 본문 추출 |
| 3 | 문단 순서 유지 | ✅ 가능 | 레코드/XML 문서 순서 그대로 `paragraph.index` 부여 |
| 4 | 표 검색 가능 보존 | ✅ 가능 | 행·열 주소 기반 grid 복원, 2,945셀 규모까지 확인 |
| 5 | 한글/숫자/특수문자 | ✅ 이상 없음 | 성공 66건 / 257,689자에서 U+FFFD(�) 0개. 독립 구현체 대비 토큰 결손 0.02% |
| 6 | page anchor | ❌ **불가** | 아래 §6 — 66건 중 0건 |
| 6 | paragraph anchor | ✅ 항상 확보 | 66건 중 66건 |
| 6 | section_title anchor | ⚠️ 제한적 | 66건 중 6건 (9%) |
| 7 | 암호화/손상/스캔 실패 분류 | ✅ 가능 | ENCRYPTED 2, OCR_REQUIRED 6, EMPTY_DOCUMENT 24 |
| 8 | 대용량 worker 안정성 | ✅ 가능 | 7.1 MB / 838문단 / 110표 → 651 ms, peak 34 MB |
| 9 | 결정성(determinism) | ✅ 확인 | 프로세스 5회 + 파일당 3회, 해시 완전 일치 |
| 10 | parser 선정 판단 | ✅ 가능 | §12 권고 |

### 가장 중요한 단서

**이번 측정은 사내 문서가 아니라 공개 third-party 코퍼스(98건)에서 수행했다.**
사내 공유폴더 문서로 재측정하기 전에는 이 결과를 사내 문서 호환성의 증거로 사용할 수 없다.
특히 성공한 HWP 5.x 파일은 6건뿐이고 그중 표를 가진 파일은 1건이므로, **HWP 표 처리의 근거는 아직 얇다.**

---

## 2. Tested Environment

| 항목 | 값 |
|---|---|
| OS | Linux 5.15.0-190-generic (x86_64, glibc 2.35) |
| Python | 3.10.12 |
| 구현 패키지 | `document_processing` 0.1.0 (본 PoC에서 작성) |
| HWP 컨테이너 의존성 | `olefile` 0.47 (BSD-2-Clause) |
| HWPX 의존성 | 없음 — 표준 라이브러리 `zipfile` + `xml.etree.ElementTree` |
| 테스트 | `pytest` 7.x |

전체 원시 결과: [`artifacts/hwp-poc-results.json`](../../artifacts/hwp-poc-results.json)

### 평가 코퍼스

사내 문서를 확보할 수 없어 **공개 third-party 코퍼스**로 측정했다. 저장소에는 커밋하지 않았고 재현 방법은
[`tests/fixtures/README.md`](../../tests/fixtures/README.md)에 기록했다.

| 출처 | 건수 | 성격 |
|---|---|---|
| `github.com/hahnlee/hwp-rs` 테스트 파일 | 16 × `.hwp` | 한글과컴퓨터 포맷 명세 문서 원본, 네이버 작성 리포트, 기능별 probe |
| `github.com/airmang/python-hwpx` 예제/테스트 | 82 × `.hwpx` | 정부·대학 서식, 공고문, HWPX 호환성 코퍼스 |
| 합계 | **98** | |

---

## 3. Parser 후보 검토

### 3.1 비교표

| 후보 | 지원 포맷 | 라이선스 | 최근 릴리스 | Python 호환 | HWP | HWPX | 표 | 페이지 | 설치 난이도 | 판정 |
|---|---|---|---|---|---|---|---|---|---|---|
| **본 PoC 자체 구현** | HWP 5.x + HWPX | 사내 코드 | — | 3.10+ | ✅ | ✅ | ✅ | ❌ | 낮음 (olefile 1개) | **채택 권고** |
| `python-hwpx` 6.3.0 | HWPX | Apache-2.0 | 2026-08-22 | 3.10–3.14 | ❌ | ✅ | ✅ | ❌ | 낮음 | 교차검증용 후보 |
| `libhwp` 0.2.0 | HWP 5.x | Apache-2.0 | 2022-11-11 | 3.7+ | ✅ | ❌ | ✅ | ❌ | 중간 (Rust 바이너리) | 조건부 · §11 확인 필요 |
| `pyhwp` 0.1b15 | HWP 5.x | **AGPL-3.0+** | 2020-05-30 | ~3.8 (선언) | ✅ | ❌ | 부분 | ❌ | 중간 | **비권고** (라이선스) |
| `hwpx` 1.1.1 (PyPI) | — | MIT (보일러플레이트) | 2025-08-28 | 3.9+ | ❌ | ❌ | ❌ | ❌ | — | **설치 금지** · §11 |
| `hwp5` 0.1.0 (PyPI) | — | MIT | 2026-05-28 | 3.9+ | ❌ | ❌ | ❌ | ❌ | — | **설치 금지** · §11 |

### 3.2 후보별 메모

**`pyhwp`** — HWP 5.x 파서로는 가장 오래된 레퍼런스지만 두 가지 문제가 있다.
(1) **AGPL-3.0-or-later**. AGPL §13은 네트워크로 소프트웨어와 상호작용하는 사용자에게 소스 제공 의무를 발생시킨다.
사내 임직원이 접속하는 웹 서비스가 여기에 해당하는지는 법무 판단 사항이며, **판단 없이 production dependency로 확정할 수 없다.**
(2) 2020년 5월 이후 릴리스가 없고 선언된 Python 지원이 3.8까지다.

**`libhwp`** — Apache-2.0이고 Rust로 작성되어 성능은 유리할 수 있다. 다만 배포 wheel에
컴파일된 `.so`(5.1 MB) 하나만 들어 있고 소스가 포함되어 있지 않으며, PyPI 메타데이터에 프로젝트 URL이 없다.
사내 반입 전 **소스 저장소 확인 및 재빌드 가능성 검증**이 필요하다.

**`python-hwpx`** — HWPX 전용. 활발히 유지보수되고(71 릴리스, 최신 2026-08) 문서화도 되어 있다.
읽기뿐 아니라 편집·생성까지 지원하므로 향후 "정해진 양식의 보고서 자동 작성"(기능명세 §19) 단계에서 재검토 가치가 있다.
이번 PoC의 HWPX 파서는 이 라이브러리를 쓰지 않고 표준 라이브러리로 직접 구현했다 — HWPX는 ZIP+XML이라 의존성을 추가할 이유가 없다.
다만 **자체 구현의 추출 누락을 검증하는 독립 레퍼런스로는 실제로 사용했다**(§4 교차 검증). 결과적으로 자체 구현이 상위집합이었다.

**자체 구현을 택한 이유** — HWPX는 개방 포맷이라 표준 라이브러리로 충분하고, HWP 5.x는 컨테이너 판독에만
`olefile`(BSD-2, 성숙·광범위 사용)을 쓰고 레코드 해석은 한글과컴퓨터가 공개한 "한글 문서 파일 형식 5.0" 명세를 따라 직접 구현했다.
결과적으로 **라이선스 위험이 있는 의존성이 0개**이고, 파서 교체가 필요해질 경우에도 `DocumentParser` 인터페이스 뒤에서 바꿀 수 있다.

---

## 4. Test Cases

공개 코퍼스 98건 전수 결과의 대표 행이다. 전체는 `artifacts/hwp-poc-results.json`에 있다.

| File | Format | Text | Paragraph Order | Table | Korean | Anchor | Result |
|---|---|---|---|---|---|---|---|
| `error__20250808__재난안전종합상황_분석_및_전망.hwpx` | hwpx | ✅ 60,183자 | ✅ 838문단 | ✅ 110표 / 2,945셀 | ✅ | para | SUCCESS |
| `gov_donation_report_form.hwpx` | hwpx | ✅ | ✅ 258문단 | ✅ 56표 / 1,582셀 | ✅ | para | SUCCESS |
| `mfds_admin_notice.hwpx` (식약처 공고) | hwpx | ✅ | ✅ 54문단 | ✅ 3표 | ✅ `「」·제2025-477호` | para | SUCCESS |
| `seoul_sihaengmun.hwpx` (서울시 시행문) | hwpx | ✅ | ✅ | ✅ 4표 (병합 포함) | ✅ | para | SUCCESS |
| `blank_form_1-2hak.hwpx` | hwpx | ✅ | ✅ 184문단 | ✅ 37표 / 728셀 | ✅ | para | SUCCESS |
| `irb_form_blank.hwpx` (수기 작성 HWPX) | hwpx | ✅ | ✅ | ⚠️ 주소 없음 → 위치 fallback | ✅ | para | SUCCESS + warning |
| `annual_report.hwp` (2.1 MB) | hwp | ✅ 4,225자 | ✅ 160문단 | — (표 없음) | ✅ | para | SUCCESS |
| `work_report.hwp` (주간업무보고서) | hwp | ✅ | ✅ 7문단 | ✅ 4표 / 70셀, 병합 포함 | ✅ | para | SUCCESS |
| `hello_world.hwp` | hwp | ✅ | ✅ | — | ✅ | para | SUCCESS |
| `한글문서파일형식_5.0_revision1.3.hwp` | hwp | ❌ | — | — | — | — | **ENCRYPTED** (배포용) |
| `한글문서파일형식_수식_revision1.3.hwp` | hwp | ❌ | — | — | — | — | **ENCRYPTED** (배포용) |
| `image_fill.hwp` | hwp | ❌ | — | — | — | — | **OCR_REQUIRED** |
| `reader_writer__SimplePicture.hwpx` | hwpx | ❌ | — | — | — | — | **OCR_REQUIRED** |
| `outline.hwp`, `tool__blank.hwpx` 외 22건 | 혼합 | ❌ | — | — | — | — | **EMPTY_DOCUMENT** (실제로 본문 없음) |

### 전수 집계

| 상태 | HWP | HWPX | 합계 |
|---|---|---|---|
| SUCCESS | 6 | 60 | **66** |
| EMPTY_DOCUMENT | 7 | 17 | 24 |
| OCR_REQUIRED | 1 | 5 | 6 |
| ENCRYPTED | 2 | 0 | 2 |
| CORRUPT / PARSE_FAILED / UNSUPPORTED_FORMAT | 0 | 0 | 0 |
| **합계** | **16** | **82** | **98** |

EMPTY_DOCUMENT 24건은 오탐이 아니다. 전량 파서 라이브러리의 단일 기능 probe 파일(도형 1개, 빈 문서 등)로,
레코드/XML을 직접 확인한 결과 실제로 본문 텍스트가 존재하지 않았다.

### 한글/숫자 보존 실측

성공 66건의 normalized text 전체에서 **U+FFFD(`�`) 0개**. 실제 추출 예:

```
「생산·수입 중단 보고대상 의료기기 및 보고 방법」(식품의약품안전처 고시 제2021-39호, 2021.5.4.)을
일부 개정함에 있어 국민에게 미리 알려 의견을 수렴하고자 ...
```

```
소규모주택정비사업 추진실적 제출('25.3분기)
국토교통부 도심주택공급협력과-3448(2025.10.13.)호와 관련하여 ...
```

한글, 가운뎃점(`·`), 낫표(`「」`), 전각 따옴표, 하이픈 포함 문서번호, 날짜, 괄호가 모두 원문 그대로 보존된다.

### 독립 구현체와의 교차 검증 (HWPX)

자체 구현의 추출 누락 여부를 확인하기 위해, 무관한 독립 구현체 `python-hwpx` 6.3.0으로 같은 82개 파일을 추출해
토큰 단위로 비교했다(격리된 venv, production 의존성에는 추가하지 않음).

| 지표 | 결과 |
|---|---|
| 비교 파일 | 54 (양쪽 모두 텍스트를 얻은 파일) |
| 참조 구현 토큰 수 | 14,313 |
| 우리 결과에서 누락된 토큰 | **3 (0.021%)** |
| 결손 0인 파일 | 52 / 54 |

누락으로 집계된 3개(`주처와`, `13간`, `11이며`)는 실제 누락이 아니라 **참조 구현 쪽의 절단 조각**이다.
원인을 XML에서 확인했다 — `<hp:t>` 안에 `<hp:tab/>` 같은 인라인 마커가 오면 그 뒤의 tail 텍스트가 참조 구현에서 유실된다.

```xml
<hp:t>   1)<hp:tab width="480"/>본 과업수행 용역에 참여하는 인원 및 기술진은 축제․공연․이벤트 등 </hp:t>
```

* 참조 구현 결과: `   1)각 분야에 학식과 ...` ← 굵은 부분이 사라짐
* 본 파서 결과: `   1)	본 과업수행 용역에 참여하는 인원 및 기술진은 축제․공연․이벤트 등 각 분야에 학식과 ...`

즉 본 파서 출력이 참조 구현의 상위집합(superset)이다. `_text_of_t()`가 자식 요소의 `tail` 텍스트를 처리하고
`<hp:tab>`/`<hp:nbSpace>`/`<hp:lineBreak>`를 각각 탭·공백·줄바꿈으로 변환하기 때문이다.

> 한계: 이 교차 검증은 **HWPX에만** 적용된다. HWP 5.x는 라이선스·공급망 문제로 배제한 후보들뿐이라
> 동일한 방식의 독립 검증을 수행하지 못했다(L4 참조).

---

## 5. Table Handling

### 보존 수준

| 항목 | HWP 5.x | HWPX |
|---|---|---|
| 셀 텍스트 누락 없음 | ✅ | ✅ |
| 행 순서 | ✅ | ✅ |
| 열 순서 | ✅ | ✅ |
| 본문·표 등장 순서 추적 | ✅ `paragraph_index` 앵커 | ✅ `paragraph_index` 앵커 |
| 병합 셀 span 값 | ✅ `row_span`/`column_span` | ✅ `row_span`/`column_span` |
| 선언 grid 크기 대조 | ✅ `declared_row_count`/`declared_column_count` | ✅ `rowCnt`/`colCnt` |
| 중첩 표 | ⚠️ 평탄화 + warning | ⚠️ 평탄화 + warning |

### 구현 근거

**HWPX** — `<hp:tbl>` → `<hp:tr>` → `<hp:tc>` 구조에서 `<hp:cellAddr colAddr/rowAddr>`와
`<hp:cellSpan colSpan/rowSpan>`을 직접 읽는다. 문서가 좌표를 스스로 선언하므로 grid 복원이 결정적이다.

**HWP 5.x** — `HWPTAG_TABLE`(77) 레코드에서 `nRows`/`nCols`와 **행별 셀 개수 배열**을 읽고,
이어지는 `HWPTAG_LIST_HEADER`(72) 레코드에서 셀 주소(col, row)와 병합 수를 읽는다.
실측 검증: `work_report.hwp`의 9×4 표는 선언 `rowSize=[1,4,4,4,4,4,4,4,4]`(합 33)이고
실제 수집된 셀도 33개로 정확히 일치했다.

> 구현 중 발견·수정한 실제 버그: 행별 셀 개수 배열의 시작 오프셋을 16바이트로 잡으면 안쪽 여백값(141)이
> 행 크기로 새어 들어온다. 정확한 오프셋은 18바이트다(property 4 + nRows 2 + nCols 2 + cellSpacing 2 + 여백 4×2).
> 회귀 테스트로 고정했다(`tests/test_hwp_records.py::TestDecodeTable::test_row_sizes_start_after_the_four_inner_margins`).

### 병합 셀 처리 방침

병합이 있다고 해서 구조가 손실된 것은 아니다. 따라서 경고를 두 단계로 나눴다.

* `MERGED_CELLS_PRESENT` — 병합이 존재함(정보성). grid는 정상 복원됨. 코퍼스 22건.
* `MERGED_CELL_STRUCTURE_LOSS` — 복원 결과가 문서 선언과 실제로 불일치할 때만 발생. **코퍼스에서 0건.**

즉 "표 구조가 손실됐는데 성공으로만 표시"되는 상황이 발생하지 않도록 두 경우를 분리했다.

### 발견된 실제 결함 (수정 완료)

`irb_form_blank.hwpx` 같은 **수기 작성 / 최소 HWPX**는 `<hp:cellAddr>` 자체가 없다.
초기 구현은 이런 셀을 건너뛰어 **표 내용을 통째로 유실**했다.
`<hp:tr>`/`<hp:tc>` 등장 순서 기반 fallback으로 수정하고 `TABLE_CELL_ADDRESS_UNREADABLE` 경고를 남기도록 했다.
같은 슬롯에 두 셀이 배치되면 덮어쓰지 않고 병합 + `TABLE_CELL_ADDRESS_COLLISION` 경고를 남긴다.

### Normalized text 표현

```
[문단]
2026년 사업계획은 다음과 같다.

[표]
부서 | 예산 | 담당자
기획실 | 300000000 | 홍길동
```

구조화 표현(`ParsedTable.cells`의 좌표·span)과 검색용 평문 표현을 **분리해서 둘 다 유지**한다.
표는 앵커 문단 위치에 렌더링되므로 원문 읽기 순서가 보존된다.

---

## 6. Source Anchor

### 결론

| Anchor | HWP 5.x | HWPX | 실측 (성공 66건) |
|---|---|---|---|
| `section_title` | ⚠️ 조건부 | ⚠️ 조건부 | **6 / 66 (9%)** |
| `page_number` | ❌ **불가** | ❌ **불가** | **0 / 66 (0%)** |
| `paragraph_index` | ✅ 항상 | ✅ 항상 | **66 / 66 (100%)** |

### page anchor를 확보할 수 없는 이유

두 포맷 모두 **페이지 번호를 저장하지 않는다.** 페이지는 렌더링 시점에 계산되는 결과이며 파일에 없다.

파일에 있는 것은 줄 배치 캐시다 — HWP는 `HWPTAG_PARA_LINE_SEG`(69), HWPX는 `<hp:linesegarray>/<hp:lineseg>`.
이 세그먼트의 flags에는 "페이지의 첫 줄인지 여부"(bit 0)가 정의되어 있어, 이론적으로는 이 비트를 누적하면
페이지 번호를 얻을 수 있다. **그래서 실제로 측정했다.**

| 측정 | 결과 |
|---|---|
| HWP 코퍼스 line segment 중 bit 0 설정된 개수 | **0** (전체 세그먼트에서 0건) |
| HWPX 코퍼스 line segment 중 bit 0 설정된 개수 | **0 / 16,745** |
| 실제로 설정된 flag 비트 | bit 17, bit 18 (그리고 일부 bit 20/21) |

즉 **레이아웃 캐시가 페이지 경계 정보를 담고 있지 않다.** 세그먼트 수 자체도 신뢰할 수 없다 —
`annual_report.hwp`는 2.1 MB 문서인데 line segment가 289개뿐이다(문단 250개와 거의 1:1). 실제 조판 결과가 아니다.

HWPX에는 `<hp:p pageBreak="1">`(강제 쪽나눔) 속성이 존재하고 코퍼스에서 73건 확인됐다.
그러나 이것만 세면 **자연 흐름에 의한 쪽넘김을 전부 놓치므로 잘못된 페이지 번호가 된다.**
PoC 지시에 따라 **추정하지 않고 `None`을 반환**한다. 모든 성공 문서에 `PAGE_NUMBER_NOT_AVAILABLE` 경고가 붙는다.

> 페이지 번호를 정말 필요로 한다면 방법은 하나뿐이다: 한글 또는 호환 렌더러로 PDF 변환 후 페이지를 매핑하는 것.
> 이는 별도 PoC 주제이며 이번 범위 밖이다.

### section_title이 제한적인 이유

문서가 스스로 선언한 개요/제목 스타일(HWP `HWPTAG_STYLE`, HWPX `<hh:style>`)에 한해서만 heading으로 인정한다.
글꼴 크기·굵기 같은 시각적 추측은 하지 않는다. 그 결과 66건 중 6건에서만 제목을 확보했다.

이는 파서 결함이 아니라 실제 문서 작성 관행의 반영이다 — 대부분의 실무 문서가 "개요 1" 같은 스타일 대신
바탕글에 수동 서식을 적용해 작성된다. 기능명세 §22가 `section_title`을 optional로 둔 것은 타당한 판단이었다.

### 결론적 권고

**`paragraph_index`를 primary citation anchor로 사용해야 한다.** `page_number`는 HWP/HWPX 경로에서 영구 NULL이다.

---

## 7. Error Handling

### 판정 가능 여부

| 오류 코드 | 판정 가능 | 근거 | 코퍼스 실측 |
|---|---|---|---|
| `UNSUPPORTED_FORMAT` | ✅ 확실 | 확장자 미등록, mimetype 불일치, 컨테이너 타입 불일치 | 0건 (단위테스트로 검증) |
| `ENCRYPTED` | ✅ 확실 | FileHeader flag bit 1(암호), bit 2(배포용), bit 4(DRM); HWPX ZIP 암호 플래그·manifest | **2건** |
| `CORRUPT` | ✅ 확실 | OLE 아님, zlib 실패, 레코드 스트림 절단, ZIP CRC 실패, XML 비정형 | 0건 (단위테스트로 검증) |
| `OCR_REQUIRED` | ⚠️ 보수적 | 텍스트 0자 **그리고** 이미지 개체 존재 | **6건** |
| `EMPTY_DOCUMENT` | ✅ 확실 | 텍스트 0자, 이미지도 없음 | **24건** |
| `PARSE_FAILED` | ✅ (fallback) | 위 어디에도 해당하지 않는 모든 예외 | 0건 |

### 설계 원칙

* **원인을 확신할 수 없으면 구체 코드로 분류하지 않는다.** `BaseParser.parse()`가 예상 못 한 예외를
  전부 `PARSE_FAILED`로 감싸므로, 라이브러리 예외가 그대로 밖으로 새지 않는다.
* **실패 시 빈 문자열을 조용히 반환하지 않는다.** 모든 실패는 예외다.
* **worker 격리 (기능명세 §21.4)**: 문서 단위 실패는 `DocumentParseError` 하나만 잡으면 되고
  배치 전체를 중단시키지 않는다. 98건 혼합 배치에서 32건이 실패했지만 러너는 정상 완료했다.

### `ENCRYPTED`의 하위 구분

`detail` 필드로 원인을 구분한다. 운영상 대응이 다르기 때문이다.

| detail | 의미 | 대응 |
|---|---|---|
| `PASSWORD_PROTECTED` | 문서 암호 설정됨 | 작성자에게 암호 해제 요청 |
| `DRM` | DRM 보호 | 보안팀 정책 확인 |
| `DISTRIBUTION_DOCUMENT` | **배포용 문서** | §10 참조 — 원본(.hwp) 재수급 필요 |

### `OCR_REQUIRED` 판정 (OCR 미구현)

OCR 엔진·이미지 인식 모델은 추가하지 않았다. 판정은 파일에서 관측 가능한 사실만 사용한다.

```
텍스트 0자 + 이미지 개체 ≥ 1  →  OCR_REQUIRED   (확실)
텍스트 0자 + 이미지 없음      →  EMPTY_DOCUMENT (확실)
텍스트 있음 + 이미지 ≥ 3 + 200자 미만 → SUCCESS + POSSIBLE_SCANNED_DOCUMENT 경고 (불확실)
```

불확실한 경우는 실패로 만들지 않고 경고로만 남긴다 — 지시대로 "판정이 불확실하면 warning".

---

## 8. Performance

98건 전수, 파일당 3회 반복 파싱 포함 총 소요 **약 2.1초**.

| 파일 | 포맷 | 크기 | 파싱 시간 | 문단 | 표 | 셀 | Peak memory |
|---|---|---|---|---|---|---|---|
| `error__20250808__재난안전종합상황...hwpx` | hwpx | 7.11 MB | **651 ms** | 838 | 110 | 2,945 | 34.3 MB |
| `annual_report.hwp` | hwp | 2.12 MB | **42 ms** | 160 | 0 | 0 | 2.6 MB |
| `gov_donation_report_form.hwpx` | hwpx | 1.79 MB | 180 ms | 258 | 56 | 1,582 | 11.9 MB |
| `error__20230728__test.hwpx` | hwpx | 0.24 MB | 179 ms | 463 | 44 | 1,046 | 8.4 MB |
| `blank_form_1-2hak.hwpx` | hwpx | 0.14 MB | 125 ms | 184 | 37 | 728 | 6.8 MB |
| `A_form.hwpx` | hwpx | 0.43 MB | 31 ms | 101 | 14 | 64 | 1.5 MB |
| `work_report.hwp` | hwp | 0.03 MB | 6 ms | 7 | 4 | 70 | 0.1 MB |

| 포맷 | 건수 | 중앙값 | 최대 | 중앙 처리량 |
|---|---|---|---|---|
| HWP 5.x | 6 | 2.5 ms | 42 ms | 10.4 MB/s |
| HWPX | 60 | 7.0 ms | 651 ms | 2.5 MB/s |

> 수치는 단일 실행 측정이며 반복 시 ±10% 정도 변동한다. 절대값보다 자릿수와 상대 비교로 읽을 것.

### 해석

* 파싱 시간은 파일 크기보다 **표 셀 개수**에 훨씬 민감하다. 0.24 MB / 1,046셀 문서(179 ms)가 0.43 MB / 64셀 문서(31 ms)보다 6배 느리다.
* **peak memory는 압축 해제된 XML 크기에 비례하며 파일 크기의 약 5배**다(HWPX, DOM 파싱).
  7.11 MB 파일이 34.3 MB를 쓴다. worker 메모리 산정 시 **파일 크기 × 5 + 여유**를 기준으로 잡아야 한다.
  50 MB 문서가 들어오면 250 MB급이므로 파일 크기 상한 정책이 필요하다.
* HWP 5.x가 더 빠르다(바이너리 레코드 vs XML DOM). 다만 HWP 성공 표본이 6건이라 중앙값의 신뢰구간은 넓다.
* 12 MB 문서를 외삽하면 약 1초 수준으로, 기능명세 §21.4의 worker 분리 구조에서 문제가 되지 않는다.

---

## 9. Determinism

### 검증 방법

정규 형식(canonical form)을 정의하고 SHA-256을 계산한다.

```
SHA-256( canonical_json(ParsedDocument) )
```

canonical form에서 **의도적으로 제외한 것**:

* `file_path` — 같은 바이트는 어디에 있든 같은 해시여야 한다
* 시간·머신 의존값(파싱 소요시간, 절대경로, PID)
* primitive가 아닌 metadata 값 — 우발적 비결정성이 해시에 새어들지 않도록 필터링

`warnings`는 정렬한다(발견 순서는 구현 세부사항). key는 정렬, `ensure_ascii=False`.

### 결과

| 검증 | 결과 |
|---|---|
| 파일당 3회 반복 파싱 (98건) | **66/66 성공 파일 전부 해시 일치** |
| 실패 파일 재현성 | 오류 코드·detail 동일 |
| **프로세스 5회 교차 검증** (`PYTHONHASHSEED` 1~5 무작위화) | 코퍼스 전체 다이제스트 **5회 완전 동일** |

```
corpus digest: a1fc8f72ccb06098210fa849809fde07ad885e3c3b337d4a8deeb25e1c1df353   (× 5)
```

프로세스 간 검증을 별도로 한 이유: Python의 해시 랜덤화가 dict/set 순회 순서를 바꾸므로,
in-process 3회 반복만으로는 결정성을 증명할 수 없다.

**결론: deterministic.** 동일 입력에 대해 `paragraph count`, `paragraph text`, `table count`,
`table cell text`, `normalized text hash`가 모두 안정적이다.

---

## 10. Known Limitations

구현으로 덮지 않고 그대로 기록한다.

### L1. 배포용(distribution) HWP 문서는 ingestion 불가 — **업무 영향 큼**

배포용 문서는 본문이 `BodyText`가 아니라 **암호화된 `ViewText` 스트림**에 저장된다.
FileHeader flag bit 2로 확실히 식별되며 `ENCRYPTED / DISTRIBUTION_DOCUMENT`로 분류한다.
본 PoC는 콘텐츠 보호 해제를 시도하지 않는다.

코퍼스 HWP 16건 중 2건(12.5%)이 배포용이었다. 한국 조직에서 대외 배포 문서를 이 형식으로 저장하는 관행이 흔하므로,
**사내 공유폴더에 배포용 문서가 얼마나 있는지 실측이 필요하다.** 비율이 높다면 원본 재수급 프로세스가 선행되어야 한다.

### L2. page anchor 확보 불가 (§6)

포맷의 성질이며 파서 개선으로 해결되지 않는다. 기능명세 §14의 출처 폴백 체인에 영향을 준다(§13 참조).

### L3. section_title 확보율 9%

문서가 개요 스타일을 명시적으로 쓴 경우에만 확보된다. 추측하지 않는다는 원칙의 직접적 결과다.

### L4. HWP 5.x 근거 표본 부족

공개 코퍼스에서 실제 본문을 가진 HWP는 6건, 표를 가진 것은 1건이다.
HWP 표 처리 로직은 명세 대조와 단위 테스트로 검증했으나 **실문서 폭이 좁다.**
사내 문서로 재측정하기 전에는 HWP를 production ingestion에 확정할 수 없다.

### L5. 중첩 표는 평탄화된다

셀 안의 표는 별도 `ParsedTable`로 분리되고 `NESTED_TABLE_FLATTENED` 경고가 남는다.
바깥 표와의 부모-자식 관계는 보존되지 않는다. 코퍼스 11건에서 발생했다.

### L6. HWPX peak memory ≈ 파일 크기 × 5

DOM 파싱이 원인이다. 대용량 문서 유입 시 worker 메모리 압박 가능성이 있으므로 파일 크기 상한 정책이 필요하다.
스트리밍 파싱(`iterparse`)으로 낮출 수 있으나 이번 범위 밖이다.

### L7. 머리말·꼬리말·각주·미주·메모는 본문에 포함되지 않는다

이번 구현은 본문 흐름과 도형 내 텍스트, 표 셀만 수집한다.
각주에 실질 정보가 담긴 문서가 많다면 별도 판단이 필요하다.

### L8. 수식(equation)은 텍스트로 추출되지 않는다

수식 개체는 건너뛴다. 코퍼스의 `한글문서파일형식_수식...hwp`은 배포용이라 확인 자체가 불가능했다.

### L9. 공개 코퍼스 기반 측정

**사내 문서 호환성은 아직 검증되지 않았다.** 이 보고서의 모든 수치에 적용되는 단서다.

---

## 11. Security / License Notes

### 11.1 의존성 라이선스

| 패키지 | 버전 | 라이선스 | 용도 | 사내 사용 위험 |
|---|---|---|---|---|
| `olefile` | 0.47 | **BSD-2-Clause** | HWP CFB 컨테이너 판독 | 낮음. permissive, 광범위 사용, 순수 Python |
| Python 표준 라이브러리 | 3.10+ | PSF | HWPX ZIP/XML | 없음 |
| `pytest` | 7.x | MIT | 테스트 전용 (dev) | 없음 |

**production dependency는 `olefile` 단 하나다.** HWPX는 의존성이 없다.

### 11.2 검토 후 배제한 라이브러리

| 패키지 | 배제 사유 |
|---|---|
| `pyhwp` | **AGPL-3.0-or-later.** 사내 웹 서비스에 AGPL §13이 적용되는지는 법무 판단 사항. 판단 전 확정 불가. 2020년 이후 미유지보수 |
| `libhwp` | Apache-2.0로 라이선스는 무해하나, wheel에 소스 없이 컴파일된 `.so`만 포함되고 PyPI 메타데이터에 프로젝트 URL이 없음. 사내 반입 전 소스 출처 확인 필요 |

### 11.3 ⚠️ PyPI 이름 충돌 — **설치 금지 목록**

PoC 중 발견한 사항으로, 별도 공유가 필요하다.

**`hwpx` (PyPI, 1.1.1)** — 이름과 달리 **HWP 라이브러리가 아니다.**
내용물은 PyPA sample project 보일러플레이트이며 실제 코드는 `hi()` 함수 하나뿐이다.
Homepage가 `github.com/pypa/sampleproject`로 되어 있다.
**더 위험한 점**: 최상위 패키지명 `hwpx`를 점유하므로, 정상 라이브러리 `python-hwpx`(import 이름도 `hwpx`)와
**설치 시 충돌**한다. 두 패키지를 함께 설치하면 어느 쪽이 이기는지 설치 순서에 따라 달라진다.

**`hwp5` (PyPI, 0.1.0)** — 이름과 달리 HWP와 무관하다.
Summary는 *"Find hard-work patterns over rolling 5-day windows in calendar export files"*이고
homepage가 `github.com/your-username/hwp5`(치환되지 않은 템플릿)다.
`pyhwp`의 import 패키지명이 `hwp5`이므로, `pip install hwp5`로 pyhwp를 설치하려는 시도를 가로챈다.

> 조치 권고: 사내 PyPI 미러/프록시가 있다면 두 패키지명을 차단 목록에 넣고,
> `requirements.txt`에 hash pinning을 적용할 것.

### 11.4 파서 자체의 보안 고려사항

공유폴더 파일은 신뢰할 수 없는 입력이다. 다음을 적용했다.

| 위협 | 대응 |
|---|---|
| XML 엔티티 확장 (billion laughs) | 섹션 XML에 `<!DOCTYPE`가 있으면 파싱 거부 → `PARSE_FAILED / XML_DTD_REJECTED`. 코퍼스 82건 중 DTD 선언 0건이므로 정상 문서에 영향 없음 |
| XXE (외부 엔티티) | 위와 동일하게 DTD 차단 |
| ZIP bomb | 압축 해제 크기 상한 256 MB, 초과 시 `PART_TOO_LARGE`로 거부 |
| ZIP CRC 손상 | `testzip()`으로 사전 검사 → `CORRUPT` |
| Zip-slip (경로 탈출) | 파일 추출을 하지 않음. 메모리에서만 읽는다 |
| 레코드 스트림 절단 | 명시적 경계 검사 → `CORRUPT` (무한 루프·과대 할당 없음) |
| 예상 못 한 예외의 worker 전파 | `BaseParser.parse()`가 전부 `PARSE_FAILED`로 포획 |

⚠️ **남은 항목**: `xml.etree.ElementTree`는 DTD를 차단해도 근본적으로 방어적 파서가 아니다.
production 반영 시 `defusedxml`(PSF-2.0) 도입을 검토할 것. 이번 PoC에서는 의존성을 늘리지 않기 위해 DTD 차단으로 대응했다.

⚠️ **원본 파일 무결성**: 파서는 파일을 읽기 전용으로만 연다. 기능명세 §3.1(공유폴더가 source of truth) 원칙과 일치한다.

---

## 12. Recommendation

### 추천 parser

| 대상 | 권고 | 근거 |
|---|---|---|
| **HWPX** | **자체 구현 (`inhouse-hwpx`)** | 의존성 0, 라이선스 위험 0, 표 구조 완전 복원, 코퍼스 60/60 안정 |
| **HWP 5.x** | **자체 구현 (`inhouse-hwp5`) + `olefile`** | 라이선스 위험 0, 명세 대조 검증 완료. **단 사내 문서 재측정 후 확정** |

### 추천하지 않는 parser

* **`pyhwp`** — AGPL 라이선스 리스크. 법무 승인 없이는 후보에서 제외.
* **`hwpx`, `hwp5` (PyPI)** — HWP 라이브러리가 아니다. 설치 금지 (§11.3).
* **`libhwp`** — 소스 출처 확인 전 보류. 자체 구현 성능이 부족할 경우에 한해 재검토.

### production ingestion 가능 여부

| 포맷 | 판정 | 조건 |
|---|---|---|
| **HWPX** | ✅ **가능** | page anchor 미지원을 설계에 반영할 것 |
| **HWP 5.x (일반)** | ⚠️ **조건부 가능** | 사내 문서 표본으로 재측정 필요 (L4) |
| **HWP 5.x (배포용)** | ❌ **불가** | 원본 재수급 프로세스 필요 (L1) |

### 필요한 fallback

1. **citation anchor**: `section_title` → `paragraph_index` (page_number 단계는 HWP/HWPX에서 항상 건너뛰게 됨)
2. **배포용 HWP**: `ENCRYPTED / DISTRIBUTION_DOCUMENT`로 표시하고 **운영자에게 원본 요청 알림**을 띄우는 흐름
3. **스캔 문서**: `OCR_REQUIRED` 큐에 적재. OCR은 기능명세 §6.2대로 후순위
4. **확장자 오기재**: `.hwp`인데 실제 HWPX인 경우가 실무에서 흔하다. 현재는 `UNSUPPORTED_FORMAT`로 명확히 안내하지만,
   **컨테이너 시그니처 기반 자동 재라우팅**을 File Sync 단계에 넣는 것을 권고한다

---

## 13. Impact on Main Architecture

기존 기능명세/DB 스키마에 **영향을 주는 부분만** 기록한다. 문서 자체는 수정하지 않았다.

### 13.1 `page_number`를 신뢰할 수 없음 → primary anchor 변경 권고

기능명세 §14의 폴백 체인:

```
section_title → page_number → paragraph_index → chunk_index
```

HWP/HWPX 경로에서 `page_number`는 **영구적으로 NULL**이고 `section_title`은 9%다.
실질적으로 다음과 같이 동작한다.

```
section_title (9%) → paragraph_index (100%)
```

`chunks.page_number INT` 컬럼은 스키마상 nullable이므로 **DDL 변경은 불필요**하다.
다만 **UI/출처 문자열이 `p.14` 같은 표기에 의존하지 않도록** 설계되어야 한다.
기능명세 §14 예시 `> 출처: 2026년 사업계획서.hwp — 기획조정실, p.14`는 HWP/HWPX에서 재현 불가능하다.

권고 표기:

```
출처: 2026년 사업계획서.hwp — 문단 142
출처: 2026년 사업계획서.hwp — 사업예산 > 문단 142   (section_title 확보 시)
```

`chunks.paragraph_start` / `paragraph_end`는 이미 스키마에 존재하므로 그대로 사용 가능하다.

### 13.2 표 구조가 안정적 → table-aware chunking 도입 가능

행·열 관계와 병합 정보가 보존되므로, chunker에서 다음이 가능하다.

* 표를 문단 중간에서 자르지 않고 표 단위로 chunk 경계 설정
* 각 행 chunk에 헤더 행을 반복 포함시켜 검색 품질 향상

`chunking_version`이 이미 `document_revisions`에 있으므로 스키마 변경 없이 도입 가능하다.
**단 PoC 단계에서 chunking을 확정하지 않는다** — 지시대로 후속 작업으로 넘긴다.

### 13.3 parser 라이선스 → 후보 변경 불필요

자체 구현 + `olefile`(BSD-2)로 라이선스 리스크가 없다.
`document_revisions.parser_name` / `parser_version`에 기록할 값:

```
parser_name    = "inhouse-hwp5"  |  "inhouse-hwpx"
parser_version = "0.1.0"
```

정규화 규칙도 별도 버전(`NORMALIZER_VERSION = "1.0.0"`)을 가지며 해시에 포함된다.
정규화 규칙이 바뀌면 chunk 재생성 대상이 되므로, **`document_revisions`에 normalizer 버전을 기록할 컬럼이
없다는 점은 Open Issue로 남긴다**(§14 OI-4).

### 13.4 `parsed_structure JSONB` 활용 가능

`document_revisions.parsed_structure JSONB`에 `canonicalize(doc)` 결과를 그대로 저장하면
기능명세 §5의 "revision 재현 가능성" 요구를 충족한다. 표 좌표·span까지 포함된다.
`extracted_text TEXT`에는 `build_normalized_text(doc)` 결과를 저장한다.

---

## 14. Open Issues

기능 명세 / DB 스키마와 충돌하거나 결정이 필요한 사항. **임의로 결정하지 않고 기록만 한다.**

### OI-1. `OCR_REQUIRED`를 저장할 곳이 스키마에 없다 — **충돌**

* 기능명세 §6.3: *"텍스트 추출이 불가능한 스캔 문서는 `OCR_REQUIRED` 상태로 표시한다."*
* DB 스키마 `document_revisions.parse_status` CHECK 제약: `PENDING | RUNNING | SUCCESS | FAILED` **뿐이다.**
* `OCR_REQUIRED`는 스키마 어디에도 존재하지 않는다 (`database_schema.MD` 전문 검색 결과 0건).

선택지:

1. `parse_status`에 `OCR_REQUIRED` 값을 추가한다 (CHECK 제약 변경)
2. `parse_status = 'FAILED'`로 두고 별도 `parse_error_code` 컬럼에 기록한다 (OI-2와 함께 해결)

**PoC에서 결정하지 않음.** 현재 코드는 `OCRRequiredError`(`error_code = "OCR_REQUIRED"`)를 발생시킨다.

### OI-2. 구조화된 실패 코드를 저장할 컬럼이 없다 — **설계 공백**

* 현재 스키마에서 실패 원인을 담을 수 있는 곳은 `processing_jobs.error_message TEXT` 하나뿐이다.
* 자유 문자열이므로 *"ENCRYPTED 문서가 몇 건인가"* 같은 질의를 신뢰성 있게 할 수 없다.
* 기능명세 §21.3(운영 지표)과 §Ⅴ "parser 처리 실패를 운영자가 추적할 수 있다"를 충족하기 어렵다.

제안(결정 아님): `document_revisions.parse_error_code TEXT`와
`processing_jobs.error_code TEXT`를 추가하고, 값 도메인을
`document_processing.parsers.exceptions.ERROR_CODES` 6종으로 제한.

### OI-3. `page_number` 기반 출처 표기가 HWP/HWPX에서 실현 불가 — **명세 재검토 필요**

* 기능명세 §14 예시가 `p.14`를 사용한다.
* §6 측정 결과 HWP/HWPX 모두 page anchor 확보 불가.
* DOCX/PDF는 상황이 다를 수 있으나 이번 PoC 범위 밖이다.

포맷별로 출처 표기 형식을 다르게 할 것인지(PDF만 `p.N`, HWP/HWPX는 문단 번호),
아니면 전 포맷 통일할 것인지 결정이 필요하다.

### OI-4. 정규화(normalizer) 버전을 기록할 컬럼이 없다

* 스키마에는 `parser_name` / `parser_version` / `chunking_version`이 있다.
* 그러나 본문 정규화 규칙은 parser와 chunker 사이의 독립 단계이며, 규칙 변경 시 chunk 재생성 대상이 된다.
* 현재 구현은 `NORMALIZER_VERSION`을 canonical hash에 포함시키지만 DB에 기록할 자리가 없다.

`normalizer_version TEXT`를 `document_revisions`에 추가할지 결정 필요.

### OI-5. 배포용 HWP 문서 처리 정책 부재 — **업무 정책 필요**

* 기능명세 §6.1은 "HWP 5.x"를 MVP 지원 대상으로 두지만, 배포용 문서를 별도로 다루지 않는다.
* 배포용 문서는 기술적으로 ingestion이 불가능하다(L1).
* 공개 코퍼스 표본에서 HWP의 12.5%가 배포용이었다.

필요한 결정: (a) 사내 공유폴더 배포용 문서 비율 실측, (b) 원본 재수급 프로세스 정의,
(c) 재수급 불가 문서를 검색 결과에 어떻게 표시할지(제목/경로만 노출할지 완전 제외할지).

### OI-6. 확장자와 실제 포맷 불일치 파일의 처리 위치

* `.hwp` 확장자를 가진 HWPX 파일이 실무에서 흔하다.
* `documents.file_type` CHECK는 `hwp | hwpx | docx | pdf`이며, "확장자는 hwp인데 실제로는 hwpx"를 표현할 자리가 없다.
* 현재 파서는 이를 감지해 `UNSUPPORTED_FORMAT`로 명확히 실패시킨다.

File Sync 단계에서 시그니처 기반으로 `file_type`을 결정할지, 파서 레벨에서 자동 재라우팅할지 결정 필요.

---

## 15. 재현 방법

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

# 단위 테스트 (fixture 불필요)
.venv/bin/pytest tests/ -q

# 실제 문서로 통합 테스트 — tests/fixtures/README.md 참조
HWP_POC_FIXTURE_DIR=/path/to/corpus .venv/bin/pytest tests/ -q

# 단일 파일 확인
.venv/bin/python scripts/run_hwp_poc.py path/to/문서.hwpx

# 디렉터리 일괄 벤치마크
.venv/bin/python scripts/run_hwp_poc.py /path/to/corpus \
    --label "사내 표본 코퍼스" \
    --json artifacts/hwp-poc-results.json
```

---

## 16. 다음 단계 제안

PoC가 통과했다고 해서 전체 서비스 구현으로 넘어가지 않는다. 다음 순서를 제안한다.

1. **사내 문서 표본 확보 후 재측정** (최우선) — 최소 100건, HWP/HWPX 비율과 배포용 문서 비율 포함.
   `scripts/run_hwp_poc.py`를 그대로 사용할 수 있다. L4·L9·OI-5가 여기서 해소된다.
2. **Open Issues 결정** — OI-1, OI-2, OI-4는 DB 스키마 확정 전에 결론이 필요하다.
3. **DOCX / 텍스트 PDF PoC** — 같은 `DocumentParser` 인터페이스로 확장. page anchor는 PDF에서 확보 가능할 가능성이 높으므로 §13.1 결론이 달라질 수 있다.
4. **한국어 lexical search PoC** — 기능명세 부록 P1의 별도 항목(PostgreSQL simple / pg_trgm / PGroonga 비교).
5. **Chunking 전략 설계** — 1~4 결과 확정 후. table-aware chunking(§13.2) 포함.

이 다섯 가지가 끝나기 전에는 DB migration이나 ingestion architecture를 확정하지 않는다.
