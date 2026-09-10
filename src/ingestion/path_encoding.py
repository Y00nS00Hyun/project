"""Storing filesystem paths that are not valid UTF-8.

A POSIX file name is a byte string with no declared encoding. On a shared
folder created from Windows, Korean names arrive as CP949 bytes, which are not
valid UTF-8 -- and PostgreSQL ``text`` holds UTF-8 only. Python surfaces those
bytes through ``surrogateescape``: each undecodable byte becomes a lone
surrogate in U+DC80..U+DCFF, and a string containing one cannot be encoded to
UTF-8 at all. Writing it fails with UnicodeEncodeError, which is exactly what
took down the first ingestion of the real corpus: 8 files discovered, 8 errors.

Two values, deliberately separate
---------------------------------
``canonical`` is the *identity*. It round-trips to the original bytes exactly,
so a stored path always reopens the file it came from. Ordinary UTF-8 names are
kept verbatim, so the common case stays readable in the database.

``display`` is what a person reads. It may guess a legacy encoding to turn
mojibake back into Korean, and a guess is acceptable there because being wrong
costs a wrong label, not a lost file.

Never ``errors="replace"``. Two different file names would collapse onto the
same U+FFFD string, and the system would treat two distinct documents as one.
"""

from __future__ import annotations

import re
import unicodedata

#: Byte values escaped in the canonical form.
#:
#: 0x80..0xFF because those are the ones surrogateescape can produce, and 0x25
#: ('%') because it introduces an escape and would otherwise be ambiguous.
_ESCAPE_MARK = "%"
_PERCENT = 0x25

#: An escape is a percent sign and exactly two upper-case hex digits naming one
#: of the escaped byte values. Anything else after a '%' is literal text, which
#: is what keeps a name like "50%off" from being mistaken for an escape.
_ESCAPE_RE = re.compile(r"%(25|[89A-F][0-9A-F])")

#: surrogateescape maps byte b to U+DC00 + b, for b in 0x80..0xFF.
_SURROGATE_BASE = 0xDC00
_SURROGATE_MIN = 0xDC80
_SURROGATE_MAX = 0xDCFF

#: Legacy encodings tried, in order, when rendering a display name. CP949 first
#: because Windows Korean is the case this exists for; it is a superset of
#: EUC-KR, so a name written in either decodes here.
_DISPLAY_FALLBACKS = ("cp949", "euc-jp", "gb18030")


class PathEncodingError(ValueError):
    """A stored path is not a well-formed canonical representation."""


def has_undecodable_bytes(text: str) -> bool:
    """True when ``text`` carries surrogateescape bytes and cannot be UTF-8."""
    return any(_SURROGATE_MIN <= ord(ch) <= _SURROGATE_MAX for ch in text)


def to_canonical(text: str) -> str:
    """Encode a filesystem string for storage in a UTF-8 text column.

    ``text`` is what ``os.fsdecode`` produced, so undecodable bytes are lone
    surrogates. Those become ``%XX``; a literal ``%`` becomes ``%25``. A name
    made entirely of valid UTF-8 and no percent sign passes through untouched.
    """
    if not text:
        return text

    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if _SURROGATE_MIN <= code <= _SURROGATE_MAX:
            out.append(f"{_ESCAPE_MARK}{code - _SURROGATE_BASE:02X}")
        elif code == _PERCENT:
            out.append(f"{_ESCAPE_MARK}{_PERCENT:02X}")
        elif 0xD800 <= code <= 0xDFFF:
            # A surrogate that surrogateescape did not put there. It cannot be
            # encoded to UTF-8 and it does not name a byte, so there is nothing
            # honest to store.
            raise PathEncodingError("path contains an unpaired surrogate")
        else:
            out.append(ch)
    return "".join(out)


def from_canonical(canonical: str) -> str:
    """Reverse :func:`to_canonical`, back to the filesystem string.

    The result may contain surrogates again -- that is correct, because that is
    the form ``os.fsencode`` needs to reproduce the original bytes.
    """
    if not canonical:
        return canonical

    def replace(match: re.Match[str]) -> str:
        value = int(match.group(1), 16)
        return "%" if value == _PERCENT else chr(_SURROGATE_BASE + value)

    return _ESCAPE_RE.sub(replace, canonical)


def is_canonical(text: str) -> bool:
    """True when ``text`` is storable as-is and round-trips unchanged."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return to_canonical(from_canonical(text)) == text


def display_name(canonical: str) -> str:
    """A human-readable rendering of a canonical path or segment.

    Presentation only. A legacy decode that succeeds here never changes what is
    stored, so a wrong guess mislabels a row rather than losing a file.

    Works one path segment at a time, on the segment's *whole* byte string.
    Decoding only the escaped runs is not enough: CP949 for "계획서" is
    B0 E8 C8 B9 BC AD, and C8 B9 happens to be valid UTF-8 (U+0239), so Python
    hands back a real character in the middle of the mojibake. Re-encoding the
    segment recovers all six bytes and CP949 then reads the whole syllable run.
    """
    if not canonical:
        return canonical

    restored = from_canonical(canonical)
    if not has_undecodable_bytes(restored):
        return restored

    rendered = []
    for segment in restored.split("/"):
        if has_undecodable_bytes(segment):
            raw = segment.encode("utf-8", "surrogateescape")
            rendered.append(_decode_legacy(raw))
        else:
            rendered.append(segment)
    return unicodedata.normalize("NFC", "/".join(rendered))


def _decode_legacy(raw: bytes) -> str:
    """Best-effort reading of a byte run, or a visible escape if none works."""
    for encoding in _DISPLAY_FALLBACKS:
        try:
            decoded = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        # A decode that yields control characters is a false positive: those do
        # not appear in real file names.
        if any(unicodedata.category(c) == "Cc" for c in decoded):
            continue
        return decoded
    # Nothing read it. Show the bytes rather than a placeholder, so two
    # different names still look different.
    return "".join(f"%{b:02X}" for b in raw)
