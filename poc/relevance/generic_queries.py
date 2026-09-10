"""Queries where a title boost could overpower semantic relevance.

The title signal is meant to help when a query names a document. It must not
let one common word in a file name outrank a document the query is genuinely
about.

Two groups, and they answer different questions.

INERT are the short generic words the corpus's titles do not contain at all
(시스템 / 설정 / 보고 / 관리 / 문서 all score 0.000 against every title). Any
weight leaves their ranking identical to semantic-only, so they are the control:
if a sweep changes them, something other than the boost moved.

RISKY are generic words that *do* fire. "정의" matches three requirement
documents at 0.667 and "요청서" matches the proposal at 0.500 -- these are the
cases where a large weight could bury a semantically better answer.
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
class GenericQuery:
    id: str
    text: str
    #: Documents a person would accept. Several, because these queries are
    #: genuinely broad -- the test is that the ranking stays sensible, not that
    #: one exact document wins.
    acceptable: tuple[str, ...]
    fires_title_boost: bool
    basis: str


QUERIES: tuple[GenericQuery, ...] = (
    # -- Control: no title in this corpus contains these ----------------------
    GenericQuery("G1", "시스템", (REPORT, SMARTER, REQ_K, REQ_Y, RFP), False,
                 "어느 제목과도 매치되지 않음(0.000). 순위는 semantic 그대로여야 한다."),
    GenericQuery("G2", "설정", (SMARTER, DEVMAN), False,
                 "제목 매치 없음. 설치/환경설정을 다루는 매뉴얼이 적절하다."),
    GenericQuery("G3", "관리", (RFP, REQ_Y, REQ_K, REPORT), False,
                 "제목 매치 없음. 계정/프로젝트 관리 조항을 가진 문서들."),
    GenericQuery("G4", "문서", (REPORT, REQ_Y, REQ_K), False,
                 "제목 매치 없음."),

    # -- Risky: these do fire, and a heavy weight could distort them ----------
    GenericQuery("G5", "정의", (REQ_Y, REQ_K), True,
                 "요구사항정의서 3건에 0.667로 발동. 그 문서들이 맞으므로 boost가 해롭지 않다."),
    GenericQuery("G6", "요청서", (RFP,), True,
                 "제안요청서 0.500, 요구사항정의서 0.250. 낮은 쪽이 올라오면 과도 boost다."),
    GenericQuery("G7", "완료", (REPORT,), True,
                 "완료보고서 0.667."),
    GenericQuery("G8", "사용자", (USERMAN, RFP), True,
                 "사용자매뉴얼 0.750. 다만 RFP의 SFR-008이 '사용자 계정 관리'라 "
                 "semantic으로도 타당하다 — 제목이 그것을 완전히 눌러서는 안 된다."),
)
