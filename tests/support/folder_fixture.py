"""A nested shared folder for folder-tree tests.

Deliberately mixes encodings. A tree built only from UTF-8 folders would pass
while the real corpus -- Korean folders created on Windows, stored as CP949
bytes -- produced paths the database cannot hold and names nobody can read.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import builders

#: (folder segments, file name, encoding of every component)
#:
#: 비공개_프로젝트 exists so a test can prove that a project nobody may read
#: does not appear in the response at all -- not its documents, not its name.
LAYOUT = [
    (["프로젝트_A", "요구사항"], "요구사항정의서.hwpx", "utf-8"),
    (["프로젝트_A", "요구사항"], "기능명세.hwpx", "utf-8"),
    (["프로젝트_A", "완료"], "완료보고서.hwpx", "utf-8"),
    # Legacy: the same shape, written the way Windows would write it.
    (["프로젝트_B", "제안"], "제안요청서.hwpx", "cp949"),
    (["프로젝트_B", "운영"], "운영매뉴얼.hwpx", "cp949"),
    (["비공개_프로젝트", "기밀"], "임원자료.hwpx", "cp949"),
    ([], "공용안내.hwpx", "utf-8"),
]


def build(root: Path) -> dict[str, Path]:
    """Create the layout under ``root``; return path-by-label."""
    created: dict[str, Path] = {}
    for segments, filename, encoding in LAYOUT:
        directory = root
        for segment in segments:
            directory = directory / os.fsdecode(segment.encode(encoding))
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / os.fsdecode(filename.encode(encoding))
        builders.write_hwpx(target, text=f"{'/'.join(segments)} {filename} 본문입니다.")
        created["/".join([*segments, filename])] = target
    return created
