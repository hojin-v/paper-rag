"""paperrag.auth.require_api_key 단위 테스트.

require_api_key는 FastAPI 의존성이라 async def지만 본문은 동기 로직이므로,
pytest-asyncio 없이 asyncio.run()으로 직접 호출해 검증한다(이 저장소의 다른
FastAPI 테스트가 커스텀 동기 TestClient로 asyncio 플러그인을 피하는 것과 같은
이유). 실제 라우터에 제대로 연결됐는지는 tests/test_search_api.py의
test_search_requires_api_key_when_configured에서 앱 전체로 확인한다.
"""

import asyncio

import pytest
from fastapi import HTTPException

from paperrag.auth import require_api_key
from paperrag.config import Settings


def _settings(api_key: str | None) -> Settings:
    return Settings(_env_file=None, api_key=api_key)


def test_no_api_key_configured_allows_any_request() -> None:
    """PAPERRAG_API_KEY 미설정(기본값)이면 헤더·쿼리 둘 다 없어도 통과해야 한다."""
    asyncio.run(require_api_key(x_api_key=None, api_key=None, settings=_settings(None)))


def test_correct_header_passes() -> None:
    asyncio.run(require_api_key(x_api_key="secret", api_key=None, settings=_settings("secret")))


def test_correct_query_param_passes() -> None:
    asyncio.run(require_api_key(x_api_key=None, api_key="secret", settings=_settings("secret")))


@pytest.mark.parametrize(
    ("header_value", "query_value"),
    [(None, None), ("wrong", None), (None, "wrong"), ("wrong", "wrong")],
)
def test_missing_or_wrong_key_is_rejected(
    header_value: str | None, query_value: str | None
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            require_api_key(
                x_api_key=header_value, api_key=query_value, settings=_settings("secret")
            )
        )
    assert exc_info.value.status_code == 401
