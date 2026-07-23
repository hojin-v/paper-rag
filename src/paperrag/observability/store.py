"""`llm_calls` 테이블 기록/조회.

기록 실패가 LLM 호출 자체(본 기능)를 막으면 안 된다 — `concurrency.py`의
"가용성 우선" 원칙과 동일하게, INSERT가 실패해도 예외를 삼키고 경고 로그만
남긴다(관찰 기능은 최적화이지 필수 안전장치가 아니다).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


def record_llm_call(
    engine: Engine,
    *,
    operation: str,
    model: str,
    prompt: str,
    response: str | None,
    success: bool,
    error: str | None = None,
    latency_ms: float | None = None,
    cache_hit: bool = False,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    """LLM 호출 1건을 `llm_calls`에 기록한다. 실패해도 예외를 던지지 않는다."""
    statement = text(
        """
        INSERT INTO llm_calls (
            operation, model, prompt, response, success, error,
            latency_ms, cache_hit, prompt_tokens, completion_tokens, context
        )
        VALUES (
            :operation, :model, :prompt, :response, :success, :error,
            :latency_ms, :cache_hit, :prompt_tokens, :completion_tokens, CAST(:context AS jsonb)
        )
        """
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                statement,
                {
                    "operation": operation,
                    "model": model,
                    "prompt": prompt,
                    "response": response,
                    "success": success,
                    "error": error,
                    "latency_ms": latency_ms,
                    "cache_hit": cache_hit,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "context": _to_json(context),
                },
            )
    except Exception:
        logger.warning("LLM 호출 기록 실패 (operation=%s)", operation, exc_info=True)


def fetch_llm_calls(
    engine: Engine,
    *,
    operation: str | None = None,
    success: bool | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """최근 LLM 호출 기록을 최신순으로 조회한다(관찰 뷰어 전용)."""
    clauses = []
    params: dict[str, Any] = {"limit": limit}
    if operation:
        clauses.append("operation = :operation")
        params["operation"] = operation
    if success is not None:
        clauses.append("success = :success")
        params["success"] = success
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    statement = text(
        f"""
        SELECT id, created_at, operation, model, prompt, response, success, error,
               latency_ms, cache_hit, prompt_tokens, completion_tokens, context
        FROM llm_calls
        {where}
        ORDER BY created_at DESC
        LIMIT :limit
        """
    )
    with engine.connect() as connection:
        rows = connection.execute(statement, params).mappings().all()
    return [dict(row) for row in rows]


def _to_json(context: dict[str, Any] | None) -> str | None:
    if context is None:
        return None
    import json

    return json.dumps(context, ensure_ascii=False)
