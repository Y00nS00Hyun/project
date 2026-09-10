"""Hand-labelled relevance benchmark over the seven READY HWP documents.

The labels were written from the documents themselves -- sampling chunks across
each file and checking term occurrences with SQL -- **before** any strategy was
run. They are not derived from what the current search happens to return, which
would make the benchmark agree with whatever it was measuring.

Labelling rule
--------------
A document is relevant when it contains content that *substantively addresses*
the query, not merely a passing occurrence of a word. "보안 위약금 부과 기준"
is answered by the document carrying the actual penalty table, not by ones that
mention that a penalty exists.

A query may have several relevant documents. NO_ANSWER queries have none.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Documents, by the title stored in `documents.title`.
# ---------------------------------------------------------------------------
REPORT = "완료보고서_d251126"
USERMAN = "사용자매뉴얼-윤수현"
RFP = "첨부 1. 제안요청서"
REQ_Y = "요구사항_정의서_윤수현"
REQ_K = "요구사항_정의서_예시(KISTI)"
SMARTER = "[KISTI]SMARTer고도화-매뉴얼"
DEVMAN = "매뉴얼_윤수현"

ALL_DOCUMENTS = (REPORT, USERMAN, RFP, REQ_Y, REQ_K, SMARTER, DEVMAN)

EXACT = "exact"
PARAPHRASE = "paraphrase"
PARTIAL = "partial"
NO_ANSWER = "no_answer"

TYPE_LABELS = {
    EXACT: "Exact / near-exact",
    PARAPHRASE: "Paraphrase / synonym",
    PARTIAL: "Partial / ambiguous",
    NO_ANSWER: "No-answer",
}


@dataclass(frozen=True)
class Query:
    id: str
    text: str
    kind: str
    relevant: tuple[str, ...] = ()
    #: Why these documents, in terms of what is actually written in them.
    basis: str = ""
    note: str = ""

    @property
    def answerable(self) -> bool:
        return self.kind != NO_ANSWER


QUERIES: tuple[Query, ...] = (
    # -- Exact / near-exact: wording close to the documents' own -------------
    Query("E1", "Elasticsearch 실행", EXACT, (SMARTER, REPORT),
          "SMARTer 매뉴얼에 '8) Elasticsearch 실행' 절이 있고, 완료보고서가 같은 설치 절차를 부록으로 포함한다."),
    Query("E2", "허밍을 업로드해 AI 음악을 생성하는 기능", EXACT, (USERMAN, REQ_Y),
          "사용자매뉴얼 첫 문단이 이 문장 그대로다. 요구사항정의서는 같은 기능을 SFR로 정의한다."),
    Query("E3", "불법 도박 사이트 탐지 웹서비스 및 SNS 크롤링", EXACT, (RFP,),
          "제안요청서의 사업명 자체. '도박'은 이 문서에만 나온다."),
    Query("E4", "보안위약금 부과 기준", EXACT, (RFP,),
          "제안요청서 [별표2] 보안위약금 부과 기준(위규 수준 A~D급 표). "
          "다른 문서는 '보안위규 처리기준에 따라 조치' 정도만 언급하고 기준표가 없다."),
    Query("E5", "음악 장르 선택 기능 요구사항", EXACT, (REQ_Y,),
          "SFR-004 '음악 장르 선택 기능' 요구사항 상세."),
    Query("E6", "종합관제지원시스템 WEB GUI 요구사항", EXACT, (REQ_K,),
          "INR-002 '종합관제지원시스템 WEB GUI' 요구사항 상세."),
    Query("E7", "gradlew bootRun 서버 실행", EXACT, (DEVMAN,),
          "'7) 서버 실행 $ ./gradlew bootRun'. gradlew는 이 문서에만 나온다."),
    Query("E8", "Kafka 압축해제", EXACT, (SMARTER, REPORT),
          "'1) Kafka 압축해제' 절. 완료보고서가 같은 절차를 포함한다."),

    # -- Paraphrase: same meaning, deliberately different words --------------
    # The target vocabulary was checked against the corpus: 색인/기동/콧노래/
    # 베팅 do not occur anywhere, so a lexical route cannot reach the answer
    # through the query's own words.
    Query("P1", "검색엔진 색인 서버를 기동하는 방법", PARAPHRASE, (SMARTER, REPORT),
          "= Elasticsearch 실행. '색인'과 '기동'은 corpus에 존재하지 않는다."),
    Query("P2", "콧노래로 노래를 만들어주는 서비스 사용 방법", PARAPHRASE, (USERMAN,),
          "= 허밍 기반 음악 생성 사용 설명서. '콧노래'는 corpus에 없다."),
    Query("P3", "온라인 베팅 사이트를 자동으로 찾아내는 시스템 구축 발주", PARAPHRASE, (RFP,),
          "= 불법 도박 사이트 탐지 시스템 제안요청. '베팅'은 corpus에 없다."),
    Query("P4", "보안 규정을 어겼을 때 물어야 하는 돈", PARAPHRASE, (RFP,),
          "= 보안위약금 부과 기준. 질의에 '위약금'이라는 단어가 없다."),
    Query("P5", "노래 스타일을 고르는 화면 명세", PARAPHRASE, (REQ_Y, USERMAN),
          "= 음악 장르 선택. 요구사항정의서가 명세를, 사용자매뉴얼이 화면을 설명한다."),
    Query("P6", "관제 화면 사용자 인터페이스 요건", PARAPHRASE, (REQ_K,),
          "= 종합관제지원시스템 WEB GUI 요구사항."),
    Query("P7", "자바 백엔드를 로컬에서 띄우는 절차", PARAPHRASE, (DEVMAN,),
          "= gradlew bootRun 서버 실행. 질의에 gradlew/bootRun이 없다."),
    Query("P8", "메시지 큐 소프트웨어 설치 절차", PARAPHRASE, (SMARTER, REPORT),
          "= Kafka 설치. SMARTer 매뉴얼에는 '메시지 큐'라는 표현이 없다."),

    # -- Partial / ambiguous: short, several documents partly address it -----
    Query("A1", "보안 요구사항", PARTIAL, (RFP, REQ_Y, REQ_K),
          "세 문서 모두 보안 요구사항 절(SER-xxx, 정보보호 가이드라인)을 갖는다."),
    Query("A2", "백업 복구 방안", PARTIAL, (REQ_K, REQ_Y, REPORT),
          "요구사항정의서 두 건이 백업/복구 방안 문서화를 요구하고, 완료보고서가 실제 백업 대상 테이블 분류를 다룬다."),
    Query("A3", "사용자 계정 관리", PARTIAL, (RFP, REQ_K),
          "RFP의 SFR-008 '사용자 계정 및 접근 권한 관리', REQ_K의 발주기관 전산망 계정 등록 조항."),
    Query("A4", "파일 다운로드", PARTIAL, (USERMAN, REQ_K),
          "사용자매뉴얼의 음악 다운로드 버튼, REQ_K의 엑셀파일 다운로드 기능."),
    Query("A5", "시스템 아키텍처", PARTIAL, (REPORT,),
          "완료보고서만 실제 아키텍처(그림 II-2, III-1)를 제시한다. "
          "요구사항정의서는 '아키텍처 다이어그램을 산출물로 제출하라'는 요구일 뿐 아키텍처가 아니다."),
    Query("A6", "데이터베이스 설정", PARTIAL, (DEVMAN, SMARTER),
          "개발환경 매뉴얼의 MySQL 계정/권한 설정, SMARTer 매뉴얼의 MariaDB bind-address 설정."),

    # -- No-answer: plausible workplace questions this corpus cannot answer --
    Query("N1", "김치찌개 조리법", NO_ANSWER, (),
          "'김치', '조리' 모두 corpus에 없다."),
    Query("N2", "연차 휴가 신청 절차", NO_ANSWER, (),
          "'연차', '휴가' 모두 corpus에 없다. 인사 규정 문서가 없다."),
    Query("N3", "법인카드 사용 규정", NO_ANSWER, (),
          "'법인카드'가 corpus에 없다."),
    Query("N4", "개인정보 파기 절차", NO_ANSWER, (),
          "'개인정보'와 '파기'는 각각 나오지만, corpus의 '파기'는 사업 종료 후 "
          "산출물·자료 반납/파기 보안조항이지 개인정보보호법상 파기 절차가 아니다.",
          note="경계 사례. 어휘는 겹치지만 질의가 요구하는 내용은 없다."),
    Query("N5", "출장비 정산 기준", NO_ANSWER, (),
          "'출장비'가 corpus에 없다. '정산'은 제안요청서에 대가 정산 맥락으로만 나온다.",
          note="lexical distractor 포함('정산')."),
    Query("N6", "사내 동호회 지원금 신청", NO_ANSWER, (),
          "'동호회'가 corpus에 없다."),
    Query("N7", "프린터 토너 교체 방법", NO_ANSWER, (),
          "'토너'가 corpus에 없다. 사무기기 문서가 없다."),
    Query("N8", "육아휴직 급여 신청 방법", NO_ANSWER, (),
          "'육아휴직', '급여' 모두 corpus에 없다."),
)


def by_kind(kind: str) -> list[Query]:
    return [q for q in QUERIES if q.kind == kind]


def validate() -> None:
    """Sanity-check the benchmark itself."""
    assert len(QUERIES) == 30, len(QUERIES)
    counts = {k: len(by_kind(k)) for k in (EXACT, PARAPHRASE, PARTIAL, NO_ANSWER)}
    assert counts == {EXACT: 8, PARAPHRASE: 8, PARTIAL: 6, NO_ANSWER: 8}, counts
    assert len({q.id for q in QUERIES}) == 30, "duplicate query id"
    for q in QUERIES:
        assert q.basis, f"{q.id} has no stated basis"
        if q.answerable:
            assert q.relevant, f"{q.id} is answerable but has no relevant document"
            for title in q.relevant:
                assert title in ALL_DOCUMENTS, f"{q.id}: unknown document {title!r}"
        else:
            assert not q.relevant, f"{q.id} is no-answer but lists documents"


if __name__ == "__main__":
    validate()
    for kind in (EXACT, PARAPHRASE, PARTIAL, NO_ANSWER):
        print(f"\n[{TYPE_LABELS[kind]}]  {len(by_kind(kind))}건")
        for q in by_kind(kind):
            rel = ", ".join(q.relevant) if q.relevant else "(관련 문서 없음)"
            print(f"  {q.id}  {q.text}")
            print(f"      relevant: {rel}")
    print("\nvalidate(): OK")
