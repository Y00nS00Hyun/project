# Test fixtures — HWP / HWPX

This directory is **empty of documents on purpose.**

The PoC brief forbids fabricating binary fixtures, and a synthetic file tells us
nothing about how Hancom's own writer lays out a real internal document. So the
integration tests **skip** until you drop real files in here, and a skip is
reported as a skip — never as a pass.

```
tests/fixtures/
├── README.md          <- this file
├── expected/          <- expected results, one JSON per fixture (already written)
├── hwp/               <- put .hwp files here
└── hwpx/              <- put .hwpx files here
```

## Quick start

1. Author the documents below in Hangul (한글) and save them under `hwp/` and
   `hwpx/` with exactly the filenames given.
2. Run the tests:

   ```bash
   pytest tests/ -q
   ```

3. Run the PoC benchmark:

   ```bash
   python scripts/run_hwp_poc.py tests/fixtures/ --json artifacts/hwp-poc-results.json
   ```

### Keeping internal documents out of the repository

Internal documents must not be committed. Point the tests at a corpus that
lives outside the repo instead:

```bash
export HWP_POC_FIXTURE_DIR=/secure/path/to/hwp-corpus
pytest tests/ -q
python scripts/run_hwp_poc.py "$HWP_POC_FIXTURE_DIR" --json artifacts/hwp-poc-results.json
```

The directory must keep the same `hwp/` and `hwpx/` layout.

## What each fixture must contain

The expectations in `expected/*.json` are matched against the text below, so
please use these exact strings. If you would rather use your own content, edit
the matching JSON file instead of the test code — expectations are data, not
hardcoded assertions.

### `hwp/simple_text.hwp` and `hwpx/simple_text.hwpx`

Body text only. No table, no image. At least three paragraphs, with these two
lines in this order:

```
2026년 사업계획
기획조정실
```

Save the same document twice: once as `.hwp` (한글 문서), once as `.hwpx`
(한글 표준 문서). Having the same content in both formats is what lets the
report compare the two parsers fairly.

### `hwp/korean_numbers.hwp` and `hwpx/korean_numbers.hwpx`

At least three paragraphs that between them contain **all** of:

```
제1조(목적)
300,000,000원
2026-01-15
①②③
㈜한국
50.5%
```

These cover Hangul, Hanja-adjacent symbols, circled numerals, a company-mark
glyph, thousands separators, an ISO date and a decimal percentage — the
character classes most likely to break in a bad decoder.

### `hwp/table.hwp` and `hwpx/table.hwpx`

One paragraph reading `2026년 사업계획`, followed by a 3-column table with a
header row:

| 부서 | 예산 | 담당자 |
| --- | --- | --- |
| 기획조정실 | 300000000 | 홍길동 |
| 총무팀 | 120000000 | 김철수 |

Write the budget figures **without** thousands separators here, so that the
table test checks cell content rather than number formatting (the
`korean_numbers` fixture covers formatting).

### `hwpx/merged_table.hwpx`

A table whose first row is a single cell merged across every column, containing
`구분`, with normal cells below. This is the merged-cell case; the parser is
expected to warn `MERGED_CELLS_PRESENT` and still reconstruct the grid.

### `hwp/multi_section.hwp` and `hwpx/multi_section.hwpx`

Two document **sections** (구역 — insert with `쪽 > 구역 나누기`, not a plain
page break). The first section contains `첫 번째 구역`, the second contains
`두 번째 구역`, plus at least two more paragraphs overall.

### `hwp/large_document.hwp` and `hwpx/large_document.hwpx`

A realistic large internal document: **≥ 2 MB**, hundreds of paragraphs, several
tables and some embedded images. A long real report from the shared folder is
much better than generated filler — this fixture is what tells us whether an
ingestion worker stays healthy on real inputs.

### `hwp/encrypted.hwp`

Any document saved with a password (`보안 > 문서 암호 설정`).
**Do not commit the password** — the test only checks that parsing is rejected
with `ENCRYPTED`.

### `hwp/distribution.hwp`

A Hancom **distribution document** (배포용 문서: `보안 > 배포용 문서로 저장`).
Its body is stored in encrypted `ViewText` streams. Expected result is
`ENCRYPTED` with detail `DISTRIBUTION_DOCUMENT` — this parser does not attempt
to decrypt content protection. This case matters: distribution documents are
common in Korean organisations and they are **not ingestible**.

### `hwp/corrupt.hwp` and `hwpx/corrupt.hwpx`

Take a valid file and truncate it to roughly half its length:

```bash
head -c $(( $(stat -c%s good.hwpx) / 2 )) good.hwpx > tests/fixtures/hwpx/corrupt.hwpx
head -c $(( $(stat -c%s good.hwp)  / 2 )) good.hwp  > tests/fixtures/hwp/corrupt.hwp
```

### `hwp/scan_like.hwp`

A document whose pages are full-page scanned images with **no text layer** —
scan a printed page and paste each page as an image. Expected result is
`OCR_REQUIRED`. OCR itself is out of scope; this only proves such files are
detected rather than silently indexed as empty.

## Adding a new fixture

Drop the file in `hwp/` or `hwpx/`, then add a JSON file to `expected/`:

```json
{
  "filename": "hwpx/my_case.hwpx",
  "format": "hwpx",
  "description": "what this document exercises",
  "expect_status": "SUCCESS",
  "expected": {
    "min_paragraphs": 3,
    "min_tables": 1,
    "min_sections": 1,
    "contains_text": ["문자열"],
    "ordered_text": ["먼저", "나중"],
    "table_contains": ["셀 내용"],
    "expect_warnings": ["MERGED_CELLS_PRESENT"],
    "table_grid": {
      "table_index": 0,
      "min_rows": 3,
      "min_columns": 3,
      "row_0": ["부서", "예산", "담당자"]
    },
    "max_parse_time_ms": 30000
  }
}
```

For a fixture that is expected to fail, drop `expected` and use:

```json
{
  "filename": "hwp/my_bad_case.hwp",
  "expect_status": "CORRUPT",
  "expect_error_detail": "optional sub-classification"
}
```

Valid `expect_status` values are `SUCCESS` plus the error codes in
`document_processing.parsers.exceptions.ERROR_CODES`:
`UNSUPPORTED_FORMAT`, `ENCRYPTED`, `CORRUPT`, `OCR_REQUIRED`, `PARSE_FAILED`,
`EMPTY_DOCUMENT`.

The test collects `expected/*.json` automatically — no test code to edit.

## Public sample corpus used during the PoC

The PoC report's measurements were taken on a **public, third-party** corpus
(98 files) rather than on internal documents, because none were available. It is
not committed here. It is reproducible:

| Source | Files | Contents |
| --- | --- | --- |
| `github.com/hahnlee/hwp-rs` → `crates/hwp/tests/integration/**/files/` | 16 × `.hwp` | Hancom's own format-spec documents, Naver-authored reports, feature probes |
| `github.com/airmang/python-hwpx` → `examples/`, `tests/**` | 82 × `.hwpx` | Korean government/university forms, HWPX writer-compatibility corpus |

These are **not a substitute for an internal corpus** — see "Limitations" in
`docs/poc/hwp-poc-report.md`. Re-run the benchmark against real shared-folder
documents before the parser decision is finalised.
