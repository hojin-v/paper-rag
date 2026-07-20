"""
PostgreSQL(+pgvector) 연결을 위한 SQLAlchemy 엔진/세션 팩토리.

ADR-0001에 따라 이 시스템은 RDB(메타데이터·키워드)와 Vector DB(임베딩)를 별도 스토어로 분리하지
않고 PostgreSQL 단일 저장소에 통합한다. 수집 파이프라인(STEP 1~8)의 저장 단계와 검색 서비스의
조회, `/ready` 헬스체크(readiness.py)가 모두 이 모듈의 엔진/세션을 통해 DB에 접근한다.
엔진과 세션 팩토리는 프로세스 전역에서 한 번만 만들어 재사용한다(커넥션 풀 중복 생성을 피하기 위함).
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from paperrag.config import Settings, get_settings

# 모듈 레벨 싱글턴. 요청/작업마다 새 엔진을 만들면 커넥션 풀이 계속 늘어나므로 최초 1회만 생성한다.
_engine: Engine | None = None
SessionLocal: sessionmaker[Session] | None = None


def get_engine(settings: Settings | None = None) -> Engine:
    """SQLAlchemy `Engine` 싱글턴을 반환한다(없으면 생성).

    `settings`를 생략하면 `paperrag.config.get_settings()`로 전역 설정의 `database_url`을 사용한다.
    `pool_pre_ping=True`는 커넥션 풀에서 꺼낸 연결이 실제로 살아있는지 매번 가볍게 확인해, 장시간
    유휴 상태였던 PostgreSQL 연결이 끊긴 채로 재사용되는 것을 방지한다(온프레미스 장기 실행 프로세스
    에서 흔한 실패 시나리오).
    """
    global _engine
    if _engine is None:
        current_settings = settings or get_settings()
        _engine = create_engine(current_settings.database_url, pool_pre_ping=True)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    """전역 `sessionmaker` 싱글턴을 반환한다(없으면 생성).

    `autocommit=False`/`autoflush=False`로 커밋 시점을 명시적으로 제어하고,
    `expire_on_commit=False`로 커밋 후에도 조회한 객체 속성을 세션 종료 전까지 그대로 사용할 수
    있게 한다(커밋 직후 반환값을 그대로 API 응답 등에 활용하는 패턴을 지원).
    """
    global SessionLocal
    if SessionLocal is None:
        SessionLocal = sessionmaker(
            bind=get_engine(),
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
    return SessionLocal


@contextmanager
def get_session() -> Iterator[Session]:
    """with 블록 안에서 안전하게 세션을 열고 닫는 컨텍스트 매니저.

    블록이 예외 없이 끝나면 커밋하고, 예외가 발생하면 롤백한 뒤 예외를 다시 던진다. 세션은 어느
    경우든 반드시 종료해 커넥션 풀에 반환한다. 수집/검색 서비스 코드가 DB 트랜잭션 범위를 명시적으로
    관리하지 않아도 되도록 감싼 헬퍼다.
    """
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ping() -> bool:
    """`SELECT 1`로 DB에 실제 연결·질의가 가능한지 확인한다.

    `/ready`(readiness.py)와 `/health` 같은 헬스체크 엔드포인트에서 사용된다. 연결 실패나 쿼리
    오류(`SQLAlchemyError`)는 예외를 전파하지 않고 `False`로 변환해, 헬스체크 호출자가 매번 예외
    처리를 반복하지 않도록 한다.
    """
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False
