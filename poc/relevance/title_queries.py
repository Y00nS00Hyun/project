"""Title-match regression queries.

These are the cases the current ranking gets visibly wrong: a query that is
part of a document's file name must outrank documents whose names have nothing
to do with it. Labelled from the file names themselves, before running
anything.
"""

from __future__ import annotations

from dataclasses import dataclass

REPORT = "완료보고서_d251126"
USERMAN = "사용자매뉴얼-윤수현"
RFP = "첨부 1. 제안요청서"
REQ_Y = "요구사항_정의서_윤수현"
REQ_K = "요구사항_정의서_예시(KISTI)"
SMARTER = "[KISTI]SMARTer고도화-매뉴얼"
DEVMAN = "매뉴얼_윤수현"


@dataclass(frozen=True)
class TitleQuery:
    id: str
    text: str
    #: Documents whose file name contains the query. Ranking them below a
    #: document with no name match at all is the defect being measured.
    expected: tuple[str, ...]
    basis: str


QUERIES: tuple[TitleQuery, ...] = (
    TitleQuery("T1", "수현", (USERMAN, REQ_Y, DEVMAN),
               "세 파일명에 '윤수현'이 들어 있다. 본문에는 요구사항_정의서_윤수현에만 1회."),
    TitleQuery("T2", "윤수현", (USERMAN, REQ_Y, DEVMAN),
               "T1과 같은 세 문서. 더 긴 형태."),
    TitleQuery("T3", "매뉴얼", (USERMAN, SMARTER, DEVMAN),
               "파일명에 '매뉴얼'이 들어 있는 세 문서."),
    TitleQuery("T4", "요구사항 정의서", (REQ_Y, REQ_K),
               "'요구사항_정의서_'로 시작하는 두 문서. 구분자가 '_'라 부분 문자열은 아니다."),
    TitleQuery("T5", "완료보고서", (REPORT,),
               "파일명이 완료보고서_d251126."),
)
