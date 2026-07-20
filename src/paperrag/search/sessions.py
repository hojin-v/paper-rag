"""검색 2단계 인터랙션에서 쓰이는 "유사 키워드 제안" 세션 저장소.

정확 매칭에 실패하면 `/search`는 임베딩 유사도 Top-3 후보와 함께 session_id를
발급하고, 사용자가 후보 중 하나를 골라 `/search/select`를 호출하면 이 세션에서
원래 질의와 후보 목록을 복원해 대표/연관 논문 선정을 이어간다.
세션은 인메모리 dict에 보관하며 TTL(기본 30분)이 지나면 만료 처리한다.
프로세스 재시작 시 세션은 모두 사라지므로 다중 인스턴스/영속화가 필요하면 별도 구현이 필요하다.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from paperrag.search.schemas import KeywordCandidate

# suggest 세션의 기본 유효 시간. docs/guide/05-search-api.md 4단계에 명시된 대로
# 만료된 session_id로 /search/select를 호출하면 404(SearchSessionNotFound)로 처리된다.
SEARCH_SESSION_TTL = timedelta(minutes=30)


@dataclass(frozen=True)
class SuggestionSession:
    """한 번의 "유사 키워드 제안" 상태를 담는 불변 스냅샷.

    session_id로 조회 가능하며, 원래 질의(query)와 LLM이 추출한 질의 키워드
    (query_keywords), 그리고 사용자에게 제시한 후보 키워드 목록(candidates)을
    함께 보관해 두었다가 `/search/select` 호출 시 그대로 재사용한다.
    """

    session_id: str
    query: str
    query_keywords: list[str]
    candidates: list[KeywordCandidate]
    expires_at: datetime
    section_query: str | None = None


class SuggestionSessionStore:
    """suggest 세션을 만들고 조회·만료시키는 인메모리 저장소.

    별도의 백그라운드 청소 스레드 없이, `create()` 호출 시점에 만료된 세션을
    한 번씩 정리(sweep)하는 방식으로 메모리 누수를 방지한다.
    """

    def __init__(self, ttl: timedelta = SEARCH_SESSION_TTL) -> None:
        self.ttl = ttl
        self._sessions: dict[str, SuggestionSession] = {}

    def create(
        self,
        query: str,
        candidates: list[KeywordCandidate],
        query_keywords: list[str] | None = None,
        *,
        section_query: str | None = None,
    ) -> SuggestionSession:
        """새 suggest 세션을 발급한다.

        정확 매칭 실패 시 SearchService.search()가 호출하며, 새 UUID를
        session_id로 사용해 만료 시각(now + ttl)과 함께 저장한다. section_query를
        함께 저장해 두면 사용자가 이후 select()로 후보를 고를 때도 같은 섹션
        필터가 그대로 적용된다.
        """
        self._sweep()
        session_id = str(uuid4())
        session = SuggestionSession(
            session_id=session_id,
            query=query,
            query_keywords=list(query_keywords or []),
            candidates=list(candidates),
            expires_at=datetime.now(UTC) + self.ttl,
            section_query=section_query,
        )
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> SuggestionSession | None:
        """세션을 조회한다. 없거나 만료됐으면 None을 반환하고 즉시 제거한다."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.expires_at <= datetime.now(UTC):
            self._sessions.pop(session_id, None)
            return None
        return session

    def expire(self, session_id: str) -> None:
        """세션을 즉시 폐기한다(사용 완료 후 정리 등에 사용)."""
        self._sessions.pop(session_id, None)

    def _sweep(self) -> None:
        """만료 시각이 지난 세션을 모두 제거한다. create() 호출마다 실행된다."""
        now = datetime.now(UTC)
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)


def new_result_id(now: datetime | None = None) -> str:
    """검색 결과 캐시 키(result_id)를 발급한다.

    `r-YYYYMMDD-<uuid 앞 8자리>` 형식으로, 사람이 봐도 생성 날짜를 알 수 있게
    하면서 충돌 가능성은 낮게 유지한다. repository.save_result()로 저장되고
    엑셀 다운로드(GET /result/{result_id}/excel) 조회 키로 쓰인다.
    """
    current = now or datetime.now(UTC)
    return f"r-{current:%Y%m%d}-{uuid4().hex[:8]}"
