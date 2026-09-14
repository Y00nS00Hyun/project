"""Paragraph-level comparison of two revisions' extracted text.

Standard library only (difflib). No LLM, no embedding, no semantic matching:
the same two texts always produce the same result.

What is compared is the text the parser extracted, never the original file.
Formatting, layout and images do not exist at this level, so changes to them
are invisible here -- the UI says so.

A unit is one line of the normalized text that build_normalized_text wrote:
each paragraph is one line after a PARAGRAPH_MARKER, and each table row is one
line after a TABLE_MARKER. Markers themselves and rows with no cell text are
not units. A whole table is deliberately not one unit -- one edited cell would
otherwise report the entire table as removed and re-added.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from document_processing.normalize import CELL_SEPARATOR, PARAGRAPH_MARKER, TABLE_MARKER

_MARKERS = frozenset({PARAGRAPH_MARKER, TABLE_MARKER})
_SEPARATOR = CELL_SEPARATOR.strip()


def paragraph_units(text: str | None) -> list[str]:
    units: list[str] = []
    for line in (text or "").split("\n"):
        unit = line.strip()
        if not unit or unit in _MARKERS:
            continue
        if not unit.replace(_SEPARATOR, "").strip():
            continue  # a table row whose cells are all empty
        units.append(unit)
    return units


def paragraph_diff(old_text: str | None, new_text: str | None) -> tuple[list[str], list[str]]:
    """(added, removed) paragraphs, each in the order it appears in its revision.

    A paragraph that moved shows up once as removed and once as added; a
    paragraph that was edited shows up as the old version removed and the new
    version added. No attempt is made to match them character by character.
    """
    old, new = paragraph_units(old_text), paragraph_units(new_text)
    added: list[str] = []
    removed: list[str] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(old[i1:i2])
        if tag in ("replace", "insert"):
            added.extend(new[j1:j2])
    return added, removed
