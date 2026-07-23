"""`llm_calls` 기록/조회를 실제 PostgreSQL(pgserver)에 대해 검증하는 통합 테스트.

`record_llm_call`이 만드는 SQL(JSONB 캐스팅, NULL 허용 컬럼)은 페이크로는 검증할 수
없어 다른 통합 테스트(test_review_store.py)와 같은 방식(pgserver 세션 픽스처)을 쓴다.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from paperrag.observability.store import fetch_llm_calls, record_llm_call


@pytest.fixture()
def engine(pg_dsn: str) -> Iterator[Engine]:
    sqlalchemy_dsn = pg_dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    created = create_engine(sqlalchemy_dsn, pool_pre_ping=True)
    try:
        yield created
    finally:
        created.dispose()


@pytest.fixture(autouse=True)
def _clean_llm_calls(engine: Engine) -> Iterator[None]:
    yield
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM llm_calls"))


def test_record_then_fetch_round_trips_all_fields(engine: Engine) -> None:
    record_llm_call(
        engine,
        operation="paragraph_enrich",
        model="qwen2.5:7b-instruct-q4_K_M",
        prompt="요약해줘",
        response='{"summary":"ok"}',
        success=True,
        latency_ms=123.4,
        cache_hit=False,
        prompt_tokens=10,
        completion_tokens=20,
        context={"document_id": "abc123"},
    )

    rows = fetch_llm_calls(engine)

    assert len(rows) == 1
    row = rows[0]
    assert row["operation"] == "paragraph_enrich"
    assert row["success"] is True
    assert row["prompt_tokens"] == 10
    assert row["completion_tokens"] == 20
    assert row["context"] == {"document_id": "abc123"}


def test_record_failure_keeps_error_and_null_response(engine: Engine) -> None:
    record_llm_call(
        engine,
        operation="keywords",
        model="qwen2.5:7b-instruct-q4_K_M",
        prompt="키워드 뽑아줘",
        response=None,
        success=False,
        error="Timeout reading from socket",
        latency_ms=5000.0,
    )

    rows = fetch_llm_calls(engine, success=False)

    assert len(rows) == 1
    assert rows[0]["response"] is None
    assert rows[0]["error"] == "Timeout reading from socket"


def test_fetch_filters_by_operation_and_success(engine: Engine) -> None:
    record_llm_call(
        engine, operation="a", model="m", prompt="p", response="r", success=True
    )
    record_llm_call(
        engine, operation="b", model="m", prompt="p", response=None, success=False, error="x"
    )

    only_a = fetch_llm_calls(engine, operation="a")
    only_failed = fetch_llm_calls(engine, success=False)

    assert [row["operation"] for row in only_a] == ["a"]
    assert [row["operation"] for row in only_failed] == ["b"]


def test_record_failure_never_raises_when_db_unreachable() -> None:
    """가용성 우선 원칙 — 기록 실패가 LLM 호출 자체를 막으면 안 된다."""
    unreachable = create_engine(
        "postgresql+psycopg://paperrag:paperrag@localhost:1/paperrag"
    )

    record_llm_call(
        unreachable, operation="a", model="m", prompt="p", response="r", success=True
    )  # 예외 없이 조용히 실패해야 한다
