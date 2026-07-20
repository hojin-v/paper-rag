"""paperrag.concurrency.heavy_task_slot 단위 테스트.

실제 Redis에 의존하지 않도록 `_redis_client`를 손으로 만든 페이크로 교체한다
(CLAUDE.md 코드 규칙 — 외부 서비스는 페이크로 대체해 오프라인 실행 가능하게).
"""

import pytest

from paperrag.concurrency import HeavyTaskBusyError, heavy_task_slot
from paperrag.config import Settings


class _FakeRedis:
    """`redis.Redis`의 SET NX/RPUSH/BLPOP만 흉내 내는 인메모리 페이크.

    실제 BLPOP처럼 블로킹하지 않고, 토큰이 없으면 즉시 None을 반환한다 —
    이 테스트는 "대기 후 타임아웃"이 아니라 "토큰 유무에 따른 분기 로직"만
    검증하면 충분하기 때문이다.
    """

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.markers: dict[str, str] = {}

    def ping(self) -> bool:
        return True

    def set(self, key: str, value: str, nx: bool = False) -> bool:
        if nx and key in self.markers:
            return False
        self.markers[key] = value
        return True

    def rpush(self, key: str, *values: str) -> None:
        self.lists.setdefault(key, []).extend(values)

    def blpop(self, key: str, timeout: float | None = None) -> tuple[str, str] | None:
        items = self.lists.get(key)
        if items:
            return key, items.pop(0)
        return None


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_disabled_when_max_concurrency_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """heavy_task_max_concurrency<=0이면 Redis 클라이언트를 아예 만들지 않고 통과시킨다."""

    def _raise_if_called(settings: Settings) -> None:
        raise AssertionError("동시성 제한이 꺼져 있는데 Redis 클라이언트를 만들었다.")

    monkeypatch.setattr("paperrag.concurrency._redis_client", _raise_if_called)

    with heavy_task_slot(_settings(heavy_task_max_concurrency=0)):
        pass  # 예외 없이 통과해야 한다


def test_disabled_when_redis_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """redis 패키지가 없거나 연결에 실패하면(_redis_client가 None) 제한 없이 통과시킨다."""
    monkeypatch.setattr("paperrag.concurrency._redis_client", lambda settings: None)

    with heavy_task_slot(_settings(heavy_task_max_concurrency=1)):
        pass  # 예외 없이 통과해야 한다


def test_acquires_and_releases_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """토큰을 정상적으로 빌리고, 블록이 끝나면 반납해 다음 호출도 통과해야 한다."""
    fake = _FakeRedis()
    monkeypatch.setattr("paperrag.concurrency._redis_client", lambda settings: fake)
    settings = _settings(heavy_task_max_concurrency=1)

    with heavy_task_slot(settings):
        # 빌린 동안에는 토큰 리스트가 비어 있어야 한다.
        assert fake.lists["paperrag:heavy_task_tokens"] == []

    # 반납 후에는 토큰이 다시 채워져 있어야 한다.
    assert fake.lists["paperrag:heavy_task_tokens"] == ["1"]

    # 두 번째 호출도 문제없이 통과해야 한다(토큰이 정확히 반납됐다는 증거).
    with heavy_task_slot(settings):
        pass


def test_raises_busy_error_when_no_token_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """동시 실행 개수 상한을 넘겨 토큰이 없으면 HeavyTaskBusyError를 던져야 한다."""
    fake = _FakeRedis()
    monkeypatch.setattr("paperrag.concurrency._redis_client", lambda settings: fake)
    settings = _settings(heavy_task_max_concurrency=1)

    with heavy_task_slot(settings):
        # 토큰을 빌린 채로 중첩 호출하면(=이미 한도만큼 사용 중) 두 번째는 실패해야 한다.
        with pytest.raises(HeavyTaskBusyError):
            with heavy_task_slot(settings):
                pass


def test_releases_token_even_if_block_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """블록 안에서 예외가 나도 토큰은 반납되어야 한다(다음 요청이 영구히 막히지 않도록)."""
    fake = _FakeRedis()
    monkeypatch.setattr("paperrag.concurrency._redis_client", lambda settings: fake)
    settings = _settings(heavy_task_max_concurrency=1)

    with pytest.raises(ValueError):
        with heavy_task_slot(settings):
            raise ValueError("작업 중 실패")

    assert fake.lists["paperrag:heavy_task_tokens"] == ["1"]
