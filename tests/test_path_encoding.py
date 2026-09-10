"""Storing filesystem paths that are not valid UTF-8.

A POSIX name is bytes. A Korean file written from Windows arrives as CP949,
which PostgreSQL's UTF-8 text column cannot hold and psycopg refuses outright.
These tests pin the two properties that matter: the stored form is always
storable, and it reproduces the original bytes exactly.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from ingestion.path_encoding import (
    PathEncodingError,
    display_name,
    from_canonical,
    has_undecodable_bytes,
    is_canonical,
    to_canonical,
)

#: Names exercised end to end. Bytes, not str, because that is what a
#: filesystem actually stores.
NAMES = [
    ("ascii", b"plan_2026.hwp"),
    ("utf8-korean", "계획서.hwp".encode("utf-8")),
    ("cp949-korean", "계획서.hwp".encode("cp949")),
    ("cp949-long", "첨부 1. 제안요청서.hwp".encode("cp949")),
    ("percent", "보고서 50% 달성.hwp".encode("utf-8")),
    ("punctuation", "a_b (c) [d] {e} 100%.hwp".encode("utf-8")),
    ("spaces", b"  leading and trailing  .hwp"),
    ("utf8-cjk", "計劃書.hwp".encode("utf-8")),
    ("mixed-bytes", b"\xff\xfe\x80 raw.hwp"),
]


class TestCanonicalRoundTrip:
    @pytest.mark.parametrize("label,raw", NAMES, ids=[n for n, _ in NAMES])
    def test_bytes_survive_the_round_trip(self, label, raw):
        fsstr = os.fsdecode(raw)
        canonical = to_canonical(fsstr)

        # Property A: storable in a UTF-8 text column.
        canonical.encode("utf-8")

        # Property B: reproduces the original bytes exactly.
        assert os.fsencode(from_canonical(canonical)) == raw

    @pytest.mark.parametrize("label,raw", NAMES, ids=[n for n, _ in NAMES])
    def test_canonical_is_stable(self, label, raw):
        canonical = to_canonical(os.fsdecode(raw))
        assert is_canonical(canonical)
        # Encoding an already-canonical value again must not double-escape.
        assert to_canonical(from_canonical(canonical)) == canonical

    def test_plain_utf8_names_are_stored_verbatim(self):
        """The common case stays readable in the database."""
        for name in ("계획서.hwp", "plan.hwp", "프로젝트A/요구사항/명세.hwp"):
            assert to_canonical(name) == name

    def test_empty_string(self):
        assert to_canonical("") == ""
        assert from_canonical("") == ""


class TestPercentIsNotAmbiguous:
    def test_a_literal_percent_is_escaped(self):
        assert to_canonical("50%.hwp") == "50%25.hwp"
        assert from_canonical("50%25.hwp") == "50%.hwp"

    def test_a_name_that_looks_like_an_escape_survives(self):
        """"%B0" typed by a person must not decode as byte 0xB0."""
        name = "%B0%E8.hwp"
        canonical = to_canonical(name)
        assert canonical == "%25B0%25E8.hwp"
        assert from_canonical(canonical) == name

    def test_a_literal_and_a_real_escape_are_distinguishable(self):
        literal = to_canonical("%B0.hwp")                       # a person typed it
        encoded = to_canonical(os.fsdecode(b"\xb0.hwp"))        # a raw byte
        assert literal != encoded
        assert os.fsencode(from_canonical(literal)) == b"%B0.hwp"
        assert os.fsencode(from_canonical(encoded)) == b"\xb0.hwp"

    def test_percent_not_followed_by_an_escape_is_left_alone(self):
        assert from_canonical("100%off") == "100%off"
        assert from_canonical("%zz") == "%zz"


class TestNoCollisions:
    def test_different_bytes_never_share_a_canonical_form(self):
        """errors="replace" would map all of these onto the same string.

        Two documents collapsing into one identity is worse than a name that
        reads badly.
        """
        raws = [
            "계획서.hwp".encode("cp949"),
            "계획서.hwp".encode("utf-8"),
            "기획서.hwp".encode("cp949"),
            b"\xb0\xe8.hwp",
            b"\xb0\xe9.hwp",
            b"%B0%E8.hwp",
        ]
        canonicals = [to_canonical(os.fsdecode(r)) for r in raws]
        assert len(set(canonicals)) == len(raws)

    def test_two_undecodable_names_stay_distinct(self):
        a = to_canonical(os.fsdecode(b"\x80\x81.hwp"))
        b = to_canonical(os.fsdecode(b"\x80\x82.hwp"))
        assert a != b
        assert "�" not in a and "�" not in b


class TestDisplayName:
    def test_plain_utf8_is_shown_as_is(self):
        assert display_name("프로젝트A/계획서.hwp") == "프로젝트A/계획서.hwp"

    def test_cp949_is_rendered_readably(self):
        canonical = to_canonical(os.fsdecode("계획서.hwp".encode("cp949")))
        assert display_name(canonical) == "계획서.hwp"

    def test_a_cp949_folder_and_file_both_render(self):
        raw = "프로젝트_A".encode("cp949") + b"/" + "계획서.hwp".encode("cp949")
        assert display_name(to_canonical(os.fsdecode(raw))) == "프로젝트_A/계획서.hwp"

    def test_a_utf8_segment_beside_a_cp949_one_is_not_mangled(self):
        """Segments are decoded independently; one legacy folder must not
        corrupt a sibling that was already valid UTF-8."""
        raw = "混合".encode("cp949") + b"/" + "計劃.hwp".encode("utf-8")
        assert display_name(to_canonical(os.fsdecode(raw))) == "混合/計劃.hwp"

    def test_a_literal_percent_is_shown_as_a_percent(self):
        assert display_name(to_canonical("50%.hwp")) == "50%.hwp"

    def test_undecodable_bytes_fall_back_to_visible_escapes(self):
        """Never a placeholder: two unreadable names must still look different."""
        a = display_name(to_canonical(os.fsdecode(b"\xff\xfe.hwp")))
        b = display_name(to_canonical(os.fsdecode(b"\xff\xfd.hwp")))
        assert a != b
        assert "�" not in a

    def test_display_never_changes_the_stored_value(self):
        canonical = to_canonical(os.fsdecode("계획서.hwp".encode("cp949")))
        before = canonical
        display_name(canonical)
        assert canonical == before


class TestGuards:
    def test_has_undecodable_bytes(self):
        assert has_undecodable_bytes(os.fsdecode(b"\xb0.hwp"))
        assert not has_undecodable_bytes("계획서.hwp")

    def test_an_unpaired_surrogate_outside_the_byte_range_is_rejected(self):
        """A surrogate from parsed text is not a file name byte."""
        with pytest.raises(PathEncodingError):
            to_canonical("bad\ud800name")


class TestRealFilesystemRoundTrip:
    """The property that actually matters: the stored path reopens the file."""

    @pytest.mark.parametrize("label,raw", NAMES, ids=[n for n, _ in NAMES])
    def test_file_opens_and_content_matches(self, label, raw, tmp_path: Path):
        from ingestion.file_scanner import canonical_relative_path, resolve_source_path

        content = os.urandom(256)
        target = tmp_path / os.fsdecode(raw)
        target.write_bytes(content)

        stored = canonical_relative_path(target, tmp_path)
        stored.encode("utf-8")  # must be storable

        reopened = resolve_source_path(stored, tmp_path)
        assert hashlib.sha256(reopened.read_bytes()).hexdigest() == \
               hashlib.sha256(content).hexdigest()

    def test_nested_legacy_folders_round_trip(self, tmp_path: Path):
        from ingestion.file_scanner import canonical_relative_path, resolve_source_path

        folder = tmp_path / os.fsdecode("프로젝트_A".encode("cp949")) / os.fsdecode(
            "요구사항".encode("cp949")
        )
        folder.mkdir(parents=True)
        target = folder / os.fsdecode("계획서.hwp".encode("cp949"))
        target.write_bytes(b"nested")

        stored = canonical_relative_path(target, tmp_path)
        stored.encode("utf-8")
        assert resolve_source_path(stored, tmp_path).read_bytes() == b"nested"
        assert display_name(stored) == "프로젝트_A/요구사항/계획서.hwp"

    def test_a_stored_path_still_cannot_escape_the_root(self, tmp_path: Path):
        from ingestion.exceptions import PathOutsideRootError
        from ingestion.file_scanner import resolve_source_path

        for hostile in ("../outside.hwp", "/etc/passwd", "a/../../outside.hwp"):
            with pytest.raises(PathOutsideRootError):
                resolve_source_path(hostile, tmp_path)

    def test_an_escape_cannot_smuggle_a_traversal(self, tmp_path: Path):
        """%2E%2E stays literal and cannot become "..".

        Only 0x25 and 0x80-0xFF are escapes, so "%2E" is an ordinary three
        character sequence. Were the scheme a general percent-decoder, this
        would decode to ".." and walk out of the shared root.
        """
        from ingestion.file_scanner import resolve_source_path

        resolved = resolve_source_path("%2E%2E/outside.hwp", tmp_path)
        # Inside the root, under a directory literally named "%2E%2E".
        assert resolved.is_relative_to(tmp_path.resolve())
        assert "%2E%2E" in resolved.parts
        assert ".." not in resolved.parts
