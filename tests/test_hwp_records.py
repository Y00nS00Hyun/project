"""Unit tests for the HWP 5.x record layer.

These build record streams from bytes, so they run without any .hwp fixture.
They verify our decoding of the documented structures -- they say nothing about
how real Hancom documents behave; that is what the integration tests and the
corpus run in the PoC report are for.
"""

from __future__ import annotations

import struct

import pytest

from document_processing.parsers import _hwp_records as rec


def record(tag_id: int, level: int, payload: bytes) -> bytes:
    """Build one record, using the extended size form when needed."""
    if len(payload) >= 0xFFF:
        header = (tag_id & 0x3FF) | ((level & 0x3FF) << 10) | (0xFFF << 20)
        return struct.pack("<II", header, len(payload)) + payload
    header = (tag_id & 0x3FF) | ((level & 0x3FF) << 10) | (len(payload) << 20)
    return struct.pack("<I", header) + payload


def utf16(text: str) -> bytes:
    return text.encode("utf-16-le")


class TestIterRecords:
    def test_reads_tag_level_and_payload(self):
        stream = record(rec.HWPTAG_PARA_TEXT, 1, b"\x41\x00") + record(
            rec.HWPTAG_TABLE, 2, b"\x00" * 18
        )
        records = list(rec.iter_records(stream))
        assert [(r.tag_id, r.level, len(r.payload)) for r in records] == [
            (rec.HWPTAG_PARA_TEXT, 1, 2),
            (rec.HWPTAG_TABLE, 2, 18),
        ]

    def test_handles_the_extended_size_form(self):
        payload = b"\xab" * 5000
        (result,) = list(rec.iter_records(record(rec.HWPTAG_PARA_TEXT, 1, payload)))
        assert result.payload == payload

    def test_empty_stream_yields_nothing(self):
        assert list(rec.iter_records(b"")) == []

    def test_truncated_payload_raises(self):
        stream = record(rec.HWPTAG_PARA_TEXT, 1, b"\x41\x00" * 8)[:-6]
        with pytest.raises(rec.RecordStreamError):
            list(rec.iter_records(stream))

    def test_truncated_header_raises(self):
        with pytest.raises(rec.RecordStreamError):
            list(rec.iter_records(b"\x01\x02"))


class TestDecodeParaText:
    def test_plain_korean_and_digits_survive(self):
        assert rec.decode_para_text(utf16("2026년 예산 300,000,000원")) == (
            "2026년 예산 300,000,000원"
        )

    def test_paragraph_and_line_breaks_become_newlines(self):
        assert rec.decode_para_text(utf16("가\r나\n")) == "가\n나\n"

    def test_char_control_for_tab_is_kept(self):
        # 9 is an inline control (8 WCHARs) whose visible effect is a tab.
        payload = utf16("가") + struct.pack("<H", 9) + b"\x00" * 14 + utf16("나")
        assert rec.decode_para_text(payload) == "가나"

    def test_extended_control_span_is_skipped(self):
        # 11 = extended control; occupies 8 WCHARs total.
        payload = utf16("앞") + struct.pack("<H", 11) + b"\x00" * 14 + utf16("뒤")
        assert rec.decode_para_text(payload) == "앞뒤"

    def test_extended_control_placeholder_is_optional(self):
        payload = struct.pack("<H", 11) + b"\x00" * 14
        assert rec.decode_para_text(payload, object_placeholder="[O]") == "[O]"

    def test_fixed_width_space_becomes_a_space(self):
        payload = utf16("가") + struct.pack("<H", 31) + utf16("나")
        assert rec.decode_para_text(payload) == "가 나"

    def test_odd_trailing_byte_is_ignored_rather_than_crashing(self):
        assert rec.decode_para_text(utf16("가") + b"\x00") == "가"

    def test_surrogate_pair_becomes_one_astral_character(self):
        """A code point above the BMP is stored as two WCHARs.

        Decoding each unit on its own produces two lone surrogates, and a
        string containing those cannot be encoded to UTF-8 at all -- it fails
        the database write and raises inside the Rust tokenizer. Found in a
        real 9MB HWP report carrying Hancom plane-15 glyphs.
        """
        # U+F02CE (plane 15, private use) == surrogates D B80 / DECE.
        payload = utf16("앞") + struct.pack("<HH", 0xDB80, 0xDECE) + utf16("뒤")
        result = rec.decode_para_text(payload)
        assert result == "앞\U000f02ce뒤"
        assert len(result) == 3
        result.encode("utf-8")  # must not raise

    def test_plane_2_hanja_survives(self):
        # U+20000, the start of CJK Extension B. Korean legal and personal-name
        # text reaches into this plane.
        payload = struct.pack("<HH", 0xD840, 0xDC00)
        assert rec.decode_para_text(payload) == "\U00020000"

    def test_unpaired_surrogate_is_replaced_not_propagated(self):
        """Malformed input must not yield a string that cannot be encoded."""
        for payload in (
            utf16("가") + struct.pack("<H", 0xDB80),              # high, then end
            utf16("가") + struct.pack("<HH", 0xDB80, 0x0041),     # high, then 'A'
            utf16("가") + struct.pack("<H", 0xDECE) + utf16("나"),  # stray low
        ):
            result = rec.decode_para_text(payload)
            assert "\ufffd" in result
            result.encode("utf-8")  # must not raise


class TestDecodeCtrlId:
    def test_reverses_the_stored_byte_order(self):
        assert rec.decode_ctrl_id(b" lbt" + b"\x00" * 4) == "tbl "

    def test_short_payload_is_empty(self):
        assert rec.decode_ctrl_id(b"ab") == ""


class TestDecodeTable:
    @staticmethod
    def build(rows: int, cols: int, row_sizes: list[int]) -> bytes:
        return (
            struct.pack("<I", 0)
            + struct.pack("<HH", rows, cols)
            + struct.pack("<H", 0)
            + struct.pack("<hhhh", 510, 510, 141, 141)
            + b"".join(struct.pack("<H", n) for n in row_sizes)
            + struct.pack("<H", 2)
        )

    def test_reads_dimensions_and_row_sizes(self):
        props = rec.decode_table(self.build(3, 4, [1, 4, 4]))
        assert (props.row_count, props.column_count) == (3, 4)
        assert props.row_sizes == (1, 4, 4)

    def test_row_sizes_start_after_the_four_inner_margins(self):
        # Regression: reading them one 16-bit word early leaks a margin value
        # (141) into the row sizes.
        props = rec.decode_table(self.build(2, 2, [2, 2]))
        assert 141 not in props.row_sizes

    def test_short_payload_raises(self):
        with pytest.raises(rec.RecordStreamError):
            rec.decode_table(b"\x00" * 10)

    def test_truncated_row_size_array_returns_what_is_readable(self):
        payload = self.build(5, 2, [2, 2])  # declares 5 rows, supplies 2 (+1 borderfill)
        props = rec.decode_table(payload)
        assert props.row_count == 5
        assert len(props.row_sizes) < 5


class TestDecodeCell:
    @staticmethod
    def build(col: int, row: int, colspan: int, rowspan: int, paras: int = 1) -> bytes:
        return (
            struct.pack("<I", paras)
            + struct.pack("<I", 0x20)
            + struct.pack("<HHHH", col, row, colspan, rowspan)
            + b"\x00" * 24
        )

    def test_reads_address_and_spans(self):
        cell = rec.decode_cell(self.build(2, 3, 4, 1))
        assert (cell.column, cell.row, cell.column_span, cell.row_span) == (2, 3, 4, 1)

    def test_short_payload_returns_none(self):
        assert rec.decode_cell(b"\x00" * 8) is None

    def test_implausible_address_returns_none(self):
        assert rec.decode_cell(self.build(70000 % 65536, 60000, 1, 1)) is None

    def test_zero_span_returns_none(self):
        assert rec.decode_cell(self.build(0, 0, 0, 1)) is None


class TestDecodeStyleNames:
    def test_reads_both_names(self):
        payload = (
            struct.pack("<H", len("개요 1"))
            + utf16("개요 1")
            + struct.pack("<H", len("Outline 1"))
            + utf16("Outline 1")
        )
        assert rec.decode_style_names(payload) == ("개요 1", "Outline 1")

    def test_malformed_payload_returns_empty_names(self):
        assert rec.decode_style_names(struct.pack("<H", 50) + b"ab") == ("", "")


class TestDecodeParaHeader:
    def test_reads_style_id(self):
        payload = struct.pack("<IIH", 12, 0, 7) + bytes([3]) + b"\x00" * 8
        head = rec.decode_para_header(payload)
        assert head.char_count == 12
        assert head.para_shape_id == 7
        assert head.style_id == 3

    def test_short_payload_returns_none(self):
        assert rec.decode_para_header(b"\x00" * 4) is None
