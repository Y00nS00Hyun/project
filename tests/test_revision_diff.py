"""Paragraph diff: pure function tests. No database."""

from api.revision_diff import paragraph_diff, paragraph_units


def normalized(*lines: str) -> str:
    """Text in the shape build_normalized_text stores."""
    out: list[str] = []
    for line in lines:
        if line.startswith("표:"):
            out += ["[표]", *line[2:].split(";")]
        else:
            out += ["[문단]", line]
    return "\n".join(out)


def test_markers_and_empty_table_rows_are_not_units():
    text = normalized("첫 문단", "표:항목 | 값; | ", "둘째 문단")
    assert paragraph_units(text) == ["첫 문단", "항목 | 값", "둘째 문단"]


def test_an_added_paragraph_is_added():
    added, removed = paragraph_diff(normalized("A", "C"), normalized("A", "B", "C"))
    assert (added, removed) == (["B"], [])


def test_a_removed_paragraph_is_removed():
    added, removed = paragraph_diff(normalized("A", "B", "C"), normalized("A", "C"))
    assert (added, removed) == ([], ["B"])


def test_an_edited_paragraph_is_old_removed_and_new_added():
    added, removed = paragraph_diff(normalized("A", "예산 3억"), normalized("A", "예산 4억"))
    assert (added, removed) == (["예산 4억"], ["예산 3억"])


def test_identical_text_has_no_changes():
    text = normalized("A", "B", "표:x | y")
    assert paragraph_diff(text, text) == ([], [])


def test_one_edited_table_cell_reports_one_row_not_the_table():
    old = normalized("표:항목 | 금액;인건비 | 1억;재료비 | 2억")
    new = normalized("표:항목 | 금액;인건비 | 1억;재료비 | 3억")
    assert paragraph_diff(old, new) == (["재료비 | 3억"], ["재료비 | 2억"])
