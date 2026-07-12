from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from paperrag.search.schemas import KeywordCandidate

SEARCH_SESSION_TTL = timedelta(minutes=30)


@dataclass(frozen=True)
class SuggestionSession:
    session_id: str
    query: str
    query_keywords: list[str]
    candidates: list[KeywordCandidate]
    expires_at: datetime


class SuggestionSessionStore:
    def __init__(self, ttl: timedelta = SEARCH_SESSION_TTL) -> None:
        self.ttl = ttl
        self._sessions: dict[str, SuggestionSession] = {}

    def create(
        self,
        query: str,
        candidates: list[KeywordCandidate],
        query_keywords: list[str] | None = None,
    ) -> SuggestionSession:
        self._sweep()
        session_id = str(uuid4())
        session = SuggestionSession(
            session_id=session_id,
            query=query,
            query_keywords=list(query_keywords or []),
            candidates=list(candidates),
            expires_at=datetime.now(UTC) + self.ttl,
        )
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> SuggestionSession | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.expires_at <= datetime.now(UTC):
            self._sessions.pop(session_id, None)
            return None
        return session

    def expire(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def _sweep(self) -> None:
        now = datetime.now(UTC)
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)


def new_result_id(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    return f"r-{current:%Y%m%d}-{uuid4().hex[:8]}"
