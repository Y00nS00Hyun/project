"""Builders for deliberately malformed inputs used by the error-mapping tests.

These are NOT document fixtures and must never be used to claim anything about
parser quality on real documents.  They exist only to prove that our error
classification does what it says on inputs whose defect we control exactly.
Real-document behaviour is covered by the integration tests, which skip when no
real fixture is present (see tests/fixtures/README.md).
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

MINIMAL_SECTION_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<hs:sec xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"
        xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section">
  <hp:p id="1" styleIDRef="0"><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>
</hs:sec>
"""


def write_hwpx(
    path: Path,
    *,
    section_xml: str | None = None,
    extra_parts: dict[str, str] | None = None,
    include_mimetype: bool = True,
    text: str = "테스트 본문 1234",
) -> Path:
    """Write a structurally valid HWPX package."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        if include_mimetype:
            package.writestr("mimetype", "application/hwp+zip")
        package.writestr(
            "Contents/section0.xml",
            section_xml if section_xml is not None else MINIMAL_SECTION_XML.format(text=text),
        )
        for name, content in (extra_parts or {}).items():
            package.writestr(name, content)
    return path


def write_not_a_zip(path: Path) -> Path:
    path.write_bytes(b"this file is not a container at all" * 4)
    return path


def write_truncated_zip(path: Path, source: Path) -> Path:
    """Copy a valid package but cut off the end, destroying its directory."""
    data = source.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    return path


def write_ole_magic_only(path: Path) -> Path:
    """OLE signature followed by noise -- looks like HWP, is not readable."""
    path.write_bytes(OLE_MAGIC + b"\x00" * 500)
    return path


def hwp_file_header(flags: int, version: bytes = bytes([2, 2, 0, 5])) -> bytes:
    """Build a 256-byte HWP FileHeader stream with the given flag bits."""
    header = bytearray(256)
    signature = b"HWP Document File" + b"\x00" * (32 - len(b"HWP Document File"))
    header[0:32] = signature
    header[32:36] = version
    header[36:40] = struct.pack("<I", flags)
    return bytes(header)


class FakeOle:
    """Duck-typed stand-in for ``olefile.OleFileIO``.

    Lets the FileHeader flag handling be tested without fabricating a binary
    compound file.
    """

    def __init__(self, streams: dict[str, bytes]):
        self._streams = streams

    def listdir(self):
        return [name.split("/") for name in self._streams]

    def openstream(self, name):
        import io

        if isinstance(name, list):
            name = "/".join(name)
        return io.BytesIO(self._streams[name])

    def close(self):
        pass
