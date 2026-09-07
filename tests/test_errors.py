"""Unit tests for parser selection and error classification."""

from __future__ import annotations

from pathlib import Path

import pytest

from document_processing.parsers import (
    HwpParser,
    HwpxParser,
    available_parsers,
    get_parser,
    parse_document,
)
from document_processing.parsers.base import BaseParser
from document_processing.parsers.exceptions import (
    ERROR_CODES,
    CorruptDocumentError,
    DocumentParseError,
    EmptyDocumentError,
    EncryptedDocumentError,
    OCRRequiredError,
    ParseFailedError,
    UnsupportedFormatError,
)
from document_processing.parsers._hwp_records import (
    FLAG_COMPRESSED,
    FLAG_DISTRIBUTION,
    FLAG_DRM,
    FLAG_PASSWORD,
)

from support import builders


class TestParserSelection:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("a.hwp", "inhouse-hwp5"),
            ("a.HWP", "inhouse-hwp5"),
            ("a.hwpx", "inhouse-hwpx"),
            ("a.HWPX", "inhouse-hwpx"),
            ("report.2026.hwpx", "inhouse-hwpx"),
        ],
    )
    def test_selects_by_extension_case_insensitively(self, name, expected):
        assert get_parser(Path(name)).name == expected

    @pytest.mark.parametrize("name", ["a.docx", "a.pdf", "a.txt", "a", "a.hwpz"])
    def test_unknown_extension_raises_unsupported_format(self, name):
        with pytest.raises(UnsupportedFormatError) as excinfo:
            get_parser(Path(name))
        assert excinfo.value.error_code == "UNSUPPORTED_FORMAT"

    def test_every_registered_parser_satisfies_the_protocol(self):
        for parser in available_parsers():
            assert hasattr(parser, "supports") and hasattr(parser, "parse")
            assert parser.name and parser.version

    def test_missing_file_is_a_parse_error_not_an_oserror(self, tmp_path):
        with pytest.raises(DocumentParseError) as excinfo:
            parse_document(tmp_path / "nope.hwpx")
        assert excinfo.value.error_code == "PARSE_FAILED"
        assert excinfo.value.detail == "FILE_NOT_FOUND"


class TestErrorTaxonomy:
    def test_every_exception_code_is_declared(self):
        codes = {
            UnsupportedFormatError.error_code,
            EncryptedDocumentError.error_code,
            CorruptDocumentError.error_code,
            OCRRequiredError.error_code,
            ParseFailedError.error_code,
            EmptyDocumentError.error_code,
        }
        assert codes == set(ERROR_CODES)

    def test_all_errors_share_one_base_class(self):
        for cls in (
            UnsupportedFormatError,
            EncryptedDocumentError,
            CorruptDocumentError,
            OCRRequiredError,
            ParseFailedError,
            EmptyDocumentError,
        ):
            assert issubclass(cls, DocumentParseError)

    def test_str_includes_code_and_detail(self):
        error = EncryptedDocumentError("locked", detail="DISTRIBUTION_DOCUMENT")
        assert "[ENCRYPTED/DISTRIBUTION_DOCUMENT]" in str(error)


class TestUnexpectedExceptionsAreContained:
    def test_arbitrary_exception_becomes_parse_failed(self, tmp_path):
        class Exploding(BaseParser):
            name, version, extensions = "boom", "0", (".hwpx",)

            def _parse(self, file_path):
                raise RuntimeError("library blew up")

        with pytest.raises(ParseFailedError) as excinfo:
            Exploding().parse(tmp_path / "x.hwpx")
        assert excinfo.value.error_code == "PARSE_FAILED"
        assert excinfo.value.detail == "RuntimeError"

    def test_specific_errors_are_not_reclassified(self, tmp_path):
        class Locked(BaseParser):
            name, version, extensions = "locked", "0", (".hwpx",)

            def _parse(self, file_path):
                raise EncryptedDocumentError("nope")

        with pytest.raises(EncryptedDocumentError):
            Locked().parse(tmp_path / "x.hwpx")


class TestHwpxErrorMapping:
    def test_valid_package_parses(self, tmp_path):
        path = builders.write_hwpx(tmp_path / "ok.hwpx")
        doc = HwpxParser().parse(path)
        assert doc.paragraphs[0].text == "테스트 본문 1234"

    def test_non_zip_is_corrupt(self, tmp_path):
        path = builders.write_not_a_zip(tmp_path / "bad.hwpx")
        with pytest.raises(CorruptDocumentError):
            HwpxParser().parse(path)

    def test_truncated_zip_is_corrupt(self, tmp_path):
        good = builders.write_hwpx(tmp_path / "good.hwpx", text="가" * 500)
        path = builders.write_truncated_zip(tmp_path / "cut.hwpx", good)
        with pytest.raises(CorruptDocumentError):
            HwpxParser().parse(path)

    def test_ole_file_named_hwpx_is_unsupported_format(self, tmp_path):
        path = builders.write_ole_magic_only(tmp_path / "mislabelled.hwpx")
        with pytest.raises(UnsupportedFormatError) as excinfo:
            HwpxParser().parse(path)
        assert "HWP 5.x" in str(excinfo.value)

    def test_malformed_section_xml_is_corrupt(self, tmp_path):
        path = builders.write_hwpx(tmp_path / "bad.hwpx", section_xml="<hs:sec><oops>")
        with pytest.raises(CorruptDocumentError):
            HwpxParser().parse(path)

    def test_package_without_sections_is_corrupt(self, tmp_path):
        import zipfile

        path = tmp_path / "nosec.hwpx"
        with zipfile.ZipFile(path, "w") as package:
            package.writestr("mimetype", "application/hwp+zip")
            package.writestr("version.xml", "<v/>")
        with pytest.raises(CorruptDocumentError):
            HwpxParser().parse(path)

    def test_wrong_mimetype_is_unsupported_format(self, tmp_path):
        import zipfile

        path = tmp_path / "odt.hwpx"
        with zipfile.ZipFile(path, "w") as package:
            package.writestr("mimetype", "application/vnd.oasis.opendocument.text")
            package.writestr("Contents/section0.xml", "<a/>")
        with pytest.raises(UnsupportedFormatError):
            HwpxParser().parse(path)

    def test_manifest_declaring_encryption_is_encrypted(self, tmp_path):
        path = builders.write_hwpx(
            tmp_path / "enc.hwpx",
            extra_parts={
                "META-INF/manifest.xml": "<manifest><encryption-data/></manifest>"
            },
        )
        with pytest.raises(EncryptedDocumentError) as excinfo:
            HwpxParser().parse(path)
        assert excinfo.value.detail == "MANIFEST_ENCRYPTION"

    def test_dtd_is_refused(self, tmp_path):
        path = builders.write_hwpx(
            tmp_path / "dtd.hwpx",
            section_xml='<?xml version="1.0"?><!DOCTYPE sec [<!ENTITY a "b">]><sec/>',
        )
        with pytest.raises(ParseFailedError) as excinfo:
            HwpxParser().parse(path)
        assert excinfo.value.detail == "XML_DTD_REJECTED"

    def test_document_with_no_text_and_no_images_is_empty(self, tmp_path):
        path = builders.write_hwpx(tmp_path / "empty.hwpx", text="")
        with pytest.raises(EmptyDocumentError):
            HwpxParser().parse(path)

    def test_document_with_no_text_but_images_needs_ocr(self, tmp_path):
        path = builders.write_hwpx(
            tmp_path / "scan.hwpx",
            text="",
            extra_parts={"BinData/image1.png": "not really a png"},
        )
        with pytest.raises(OCRRequiredError):
            HwpxParser().parse(path)


class TestHwpFileHeaderClassification:
    """Flag handling, exercised through a duck-typed OLE container."""

    @staticmethod
    def parse_with_flags(flags: int):
        ole = builders.FakeOle(
            {
                "FileHeader": builders.hwp_file_header(flags),
                "BodyText/Section0": b"",
                "DocInfo": b"",
            }
        )
        return HwpParser()._parse_ole(ole, Path("in-memory.hwp"))

    def test_password_flag_is_encrypted(self):
        with pytest.raises(EncryptedDocumentError) as excinfo:
            self.parse_with_flags(FLAG_PASSWORD)
        assert excinfo.value.detail == "PASSWORD_PROTECTED"

    def test_drm_flag_is_encrypted(self):
        with pytest.raises(EncryptedDocumentError) as excinfo:
            self.parse_with_flags(FLAG_DRM)
        assert excinfo.value.detail == "DRM"

    def test_distribution_flag_is_encrypted(self):
        with pytest.raises(EncryptedDocumentError) as excinfo:
            self.parse_with_flags(FLAG_COMPRESSED | FLAG_DISTRIBUTION)
        assert excinfo.value.detail == "DISTRIBUTION_DOCUMENT"

    def test_missing_file_header_is_corrupt(self):
        ole = builders.FakeOle({"BodyText/Section0": b""})
        with pytest.raises(CorruptDocumentError):
            HwpParser()._parse_ole(ole, Path("in-memory.hwp"))

    def test_bad_signature_is_unsupported_format(self):
        header = bytearray(builders.hwp_file_header(0))
        header[0:32] = b"NOT A HWP FILE".ljust(32, b"\x00")
        ole = builders.FakeOle({"FileHeader": bytes(header)})
        with pytest.raises(UnsupportedFormatError):
            HwpParser()._parse_ole(ole, Path("in-memory.hwp"))

    def test_no_body_section_is_corrupt(self):
        ole = builders.FakeOle({"FileHeader": builders.hwp_file_header(0), "DocInfo": b""})
        with pytest.raises(CorruptDocumentError):
            HwpParser()._parse_ole(ole, Path("in-memory.hwp"))

    def test_undecompressable_section_is_corrupt(self):
        ole = builders.FakeOle(
            {
                "FileHeader": builders.hwp_file_header(FLAG_COMPRESSED),
                "DocInfo": b"",
                "BodyText/Section0": b"definitely not deflate data",
            }
        )
        with pytest.raises(CorruptDocumentError):
            HwpParser()._parse_ole(ole, Path("in-memory.hwp"))


class TestHwpContainerErrors:
    def test_zip_named_hwp_is_unsupported_format(self, tmp_path):
        path = builders.write_hwpx(tmp_path / "mislabelled.hwp")
        with pytest.raises(UnsupportedFormatError) as excinfo:
            HwpParser().parse(path)
        assert "HWPX" in str(excinfo.value)

    def test_random_bytes_named_hwp_is_corrupt(self, tmp_path):
        path = builders.write_not_a_zip(tmp_path / "bad.hwp")
        with pytest.raises(CorruptDocumentError):
            HwpParser().parse(path)
