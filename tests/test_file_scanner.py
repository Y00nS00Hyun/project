"""Discovery, fingerprinting and path-boundary tests.

The security cases here are the point of the module: a shared folder is not a
trusted input, and a scanner that follows a symlink or a `..` out of the root
would happily index anything on the host.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ingestion.config import IngestionConfig
from ingestion.exceptions import FileUnstableError, FileVanishedError, PathOutsideRootError
from ingestion.file_identity import fingerprint
from ingestion.file_scanner import (
    canonical_relative_path,
    resolve_source_path,
    scan_files,
)


@pytest.fixture
def root(tmp_path) -> Path:
    shared = tmp_path / "shared"
    shared.mkdir()
    return shared


def config_for(root: Path, **kwargs) -> IngestionConfig:
    return IngestionConfig(shared_root=root, **kwargs)


def write(path: Path, content: bytes = b"content") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class TestDiscovery:
    def test_finds_supported_extensions(self, root):
        write(root / "a.hwp")
        write(root / "b.hwpx")
        write(root / "c.docx")
        write(root / "d.pdf")
        found = {f.relative_path for f in scan_files(config_for(root))}
        assert found == {"a.hwp", "b.hwpx", "c.docx", "d.pdf"}

    def test_ignores_unsupported_extensions(self, root):
        write(root / "notes.txt")
        write(root / "sheet.xlsx")
        write(root / "keep.hwp")
        found = {f.relative_path for f in scan_files(config_for(root))}
        assert found == {"keep.hwp"}

    def test_recurses_into_subdirectories(self, root):
        write(root / "부서" / "2026" / "plan.hwp")
        found = [f for f in scan_files(config_for(root))]
        assert found[0].relative_path == "부서/2026/plan.hwp"

    def test_relative_path_is_posix_style(self, root):
        write(root / "a" / "b" / "c.hwp")
        (found,) = list(scan_files(config_for(root)))
        assert "\\" not in found.relative_path
        assert found.relative_path == "a/b/c.hwp"

    def test_extension_is_normalised_to_lowercase(self, root):
        write(root / "UPPER.HWP")
        (found,) = list(scan_files(config_for(root)))
        assert found.extension == "hwp"

    def test_title_strips_the_extension(self, root):
        write(root / "2026년 사업계획서.hwp")
        (found,) = list(scan_files(config_for(root)))
        assert found.title == "2026년 사업계획서"

    def test_scan_is_deterministic(self, root):
        for name in ["c.hwp", "a.hwp", "b.hwp"]:
            write(root / name)
        first = [f.relative_path for f in scan_files(config_for(root))]
        second = [f.relative_path for f in scan_files(config_for(root))]
        assert first == second == sorted(first)

    def test_empty_root_yields_nothing(self, root):
        assert list(scan_files(config_for(root))) == []

    def test_metadata_is_reported(self, root):
        write(root / "a.hwp", b"1234567890")
        (found,) = list(scan_files(config_for(root)))
        assert found.size == 10
        assert found.filename == "a.hwp"
        assert found.mtime.tzinfo is not None, "mtime must be timezone-aware (UTC)"


class TestSymlinkPolicy:
    def test_symlinked_file_is_not_followed_by_default(self, root, tmp_path):
        outside = write(tmp_path / "outside" / "secret.hwp")
        os.symlink(outside, root / "link.hwp")
        assert list(scan_files(config_for(root))) == []

    def test_symlinked_directory_is_not_descended(self, root, tmp_path):
        outside_dir = tmp_path / "outside"
        write(outside_dir / "secret.hwp")
        os.symlink(outside_dir, root / "linked")
        assert list(scan_files(config_for(root))) == []

    def test_real_files_are_still_found_alongside_symlinks(self, root, tmp_path):
        os.symlink(write(tmp_path / "out" / "x.hwp"), root / "link.hwp")
        write(root / "real.hwp")
        found = {f.relative_path for f in scan_files(config_for(root))}
        assert found == {"real.hwp"}

    def test_symlink_inside_root_is_still_skipped(self, root):
        # Even a symlink that stays inside the root is skipped: following it
        # would index the same bytes twice under two paths.
        write(root / "real.hwp")
        os.symlink(root / "real.hwp", root / "alias.hwp")
        found = {f.relative_path for f in scan_files(config_for(root))}
        assert found == {"real.hwp"}


class TestPathBoundary:
    def test_relative_path_inside_root_is_accepted(self, root):
        write(root / "a" / "b.hwp")
        assert canonical_relative_path(root / "a" / "b.hwp", root) == "a/b.hwp"

    def test_traversal_out_of_root_is_rejected(self, root, tmp_path):
        outside = write(tmp_path / "outside.hwp")
        with pytest.raises(PathOutsideRootError):
            canonical_relative_path(outside, root)

    def test_dotdot_traversal_is_rejected(self, root):
        with pytest.raises(PathOutsideRootError):
            resolve_source_path("../../etc/passwd", root)

    def test_absolute_stored_path_is_rejected(self, root):
        with pytest.raises(PathOutsideRootError):
            resolve_source_path("/etc/passwd", root)

    def test_symlink_escape_is_rejected_at_read_time(self, root, tmp_path):
        outside = write(tmp_path / "outside" / "secret.hwp")
        os.symlink(outside, root / "link.hwp")
        # Even if such a path were somehow stored, opening it is refused.
        with pytest.raises(PathOutsideRootError):
            resolve_source_path("link.hwp", root)

    def test_valid_stored_path_resolves(self, root):
        write(root / "부서" / "a.hwp")
        assert resolve_source_path("부서/a.hwp", root) == (root / "부서" / "a.hwp").resolve()

    def test_error_message_does_not_leak_the_outside_path(self, root, tmp_path):
        outside = write(tmp_path / "very-secret-location" / "x.hwp")
        with pytest.raises(PathOutsideRootError) as excinfo:
            canonical_relative_path(outside, root)
        assert "very-secret-location" not in str(excinfo.value)


class TestFingerprint:
    def test_sha256_of_content(self, root):
        import hashlib

        path = write(root / "a.hwp", b"hello")
        assert fingerprint(path).content_hash == hashlib.sha256(b"hello").hexdigest()

    def test_identical_content_gives_identical_hash(self, root):
        a = write(root / "a.hwp", b"same bytes")
        b = write(root / "sub" / "b.hwp", b"same bytes")
        assert fingerprint(a).content_hash == fingerprint(b).content_hash

    def test_size_and_mtime_are_reported(self, root):
        path = write(root / "a.hwp", b"0123456789")
        finger = fingerprint(path)
        assert finger.size == 10
        assert finger.mtime_ns > 0

    def test_missing_file_raises_vanished(self, root):
        with pytest.raises(FileVanishedError):
            fingerprint(root / "nope.hwp")

    def test_large_file_is_hashed_in_blocks(self, root):
        import hashlib

        payload = os.urandom(3 * 1024 * 1024)
        path = write(root / "big.hwp", payload)
        assert fingerprint(path).content_hash == hashlib.sha256(payload).hexdigest()

    def test_file_changed_during_read_is_rejected(self, root, monkeypatch):
        """A file being written must not be stored as a revision."""
        path = write(root / "a.hwp", b"original content")

        import ingestion.file_identity as module

        real_stat = module._stat_signature
        calls = {"n": 0}

        def shifting_stat(p):
            calls["n"] += 1
            signature = real_stat(p)
            if calls["n"] > 1:
                # Simulate the writer having changed the file mid-read.
                return (signature[0] + 5, signature[1] + 1000, signature[2])
            return signature

        monkeypatch.setattr(module, "_stat_signature", shifting_stat)
        with pytest.raises(FileUnstableError):
            fingerprint(path)
