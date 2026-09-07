"""Unit tests for the PoC evaluation harness.

Exercised with builder-made packages, not real documents -- these check the
harness's own bookkeeping (status codes, determinism flag, summary maths), not
parser quality.
"""

from __future__ import annotations

from pathlib import Path

from document_processing.poc import (
    DETERMINISM_RUNS,
    build_payload,
    evaluate_file,
    iter_target_files,
    summarize,
)

from support import builders


def test_successful_file_is_reported_with_counts(tmp_path):
    path = builders.write_hwpx(tmp_path / "ok.hwpx")
    result = evaluate_file(path)

    assert result.status == "SUCCESS"
    assert result.parser == "inhouse-hwpx"
    assert result.paragraph_count == 1
    assert result.paragraph_anchor == "AVAILABLE"
    assert result.page_anchor == "NOT_AVAILABLE"
    assert result.deterministic is True
    assert result.determinism_runs == DETERMINISM_RUNS
    assert result.parse_time_ms is not None and result.parse_time_ms >= 0
    assert result.peak_memory_bytes and result.peak_memory_bytes > 0
    assert len(result.content_hash) == 64


def test_failing_file_becomes_a_row_not_an_exception(tmp_path):
    path = builders.write_not_a_zip(tmp_path / "bad.hwpx")
    result = evaluate_file(path)

    assert result.status == "CORRUPT"
    assert result.error_message
    assert result.paragraph_count is None


def test_unsupported_extension_is_reported(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    assert evaluate_file(path).status == "UNSUPPORTED_FORMAT"


def test_path_is_recorded_relative_to_the_target(tmp_path):
    (tmp_path / "hwpx").mkdir()
    path = builders.write_hwpx(tmp_path / "hwpx" / "ok.hwpx")
    result = evaluate_file(path, relative_to=tmp_path)
    assert result.path == "hwpx/ok.hwpx"


def test_directory_walk_finds_only_supported_files(tmp_path):
    (tmp_path / "sub").mkdir()
    builders.write_hwpx(tmp_path / "a.hwpx")
    builders.write_hwpx(tmp_path / "sub" / "b.hwpx")
    (tmp_path / "notes.txt").write_text("ignored")

    found = [p.name for p in iter_target_files(tmp_path)]
    assert found == ["a.hwpx", "b.hwpx"]


def test_a_bad_file_does_not_stop_the_batch(tmp_path):
    builders.write_hwpx(tmp_path / "good.hwpx")
    builders.write_not_a_zip(tmp_path / "bad.hwpx")
    builders.write_hwpx(tmp_path / "zz_good.hwpx")

    results = [evaluate_file(p, relative_to=tmp_path) for p in iter_target_files(tmp_path)]
    summary = summarize(results)

    assert summary["file_count"] == 3
    assert summary["success_count"] == 2
    assert summary["by_status"]["CORRUPT"] == 1
    assert summary["deterministic_count"] == summary["determinism_checked_count"] == 2
    assert summary["page_anchor_available_count"] == 0
    assert summary["paragraph_anchor_available_count"] == 2


def test_payload_is_json_serialisable(tmp_path):
    import json

    builders.write_hwpx(tmp_path / "a.hwpx")
    results = [evaluate_file(p, relative_to=tmp_path) for p in iter_target_files(tmp_path)]
    payload = build_payload(results, Path(tmp_path), label="unit test")

    text = json.dumps(payload, ensure_ascii=False)
    assert json.loads(text)["corpus_label"] == "unit test"
    assert payload["environment"]["python"]
