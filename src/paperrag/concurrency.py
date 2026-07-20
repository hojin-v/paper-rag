"""Redis 기반 분산 세마포어 — 여러 프로세스에 걸쳐 "무거운 작업" 동시 실행 개수를 제한한다.

2026-07-12 실측(docs/guide/10-production-readiness.md, docs/reports/assessments/
2026-07-12-two-paper-ocr-evaluation.md)에서 Paddle OCR + BGE-M3 + Qwen2.5 7B를
동시에 상주시키면 7.5GiB급 환경에서 swap이 가득 차 요청이 실패하는 것이 확인됐다.
`threading.Semaphore` 하나로는 API 프로세스 내부의 스레드끼리만 조율할 수 있고
Celery worker(별도 프로세스, 여러 개일 수 있음)와는 자원을 공유하지 못하므로,
이미 인프라에 있는 Redis를 빌려 프로세스 경계를 넘는 카운팅 세마포어를 구현한다.

구현은 Redis 리스트를 토큰 버킷으로 쓰는 표준 패턴이다: `heavy_task_max_concurrency`개의
토큰을 미리 채워두고 `BLPOP`으로 하나를 빌리며(타임아웃 안에 못 받으면 명시적으로
포기), 작업이 끝나면 `RPUSH`로 반납한다. 최초 초기화는 `SET ... NX`로 여러
프로세스가 동시에 기동해도 정확히 한 번만 토큰을 채우도록 한다.

의도적으로 가용성을 우선한다 — redis 패키지가 없거나 Redis에 연결할 수 없으면
제한 없이 통과시킨다(이 세마포어는 최적화이지, 없으면 서비스가 멈춰야 하는 필수
안전장치가 아니다). 알려진 한계: 토큰을 빌린 프로세스가 반납 전에 크래시하면
그 토큰은 다음 프로세스 재시작 전까지 영구히 사라진다(모든 요청이 결국 타임아웃
후 거부되는 방향으로만 영향을 준다 — 조용히 정합성이 깨지는 종류의 버그는 아니다).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from paperrag.config import Settings, get_settings

_TOKEN_KEY = "paperrag:heavy_task_tokens"
_INIT_MARKER_KEY = f"{_TOKEN_KEY}:initialized"


class HeavyTaskBusyError(RuntimeError):
    """세마포어 대기 시간 안에 토큰을 얻지 못했을 때(=무거운 작업 슬롯이 이미 전부 사용 중)."""


def _redis_client(settings: Settings) -> Any | None:
    """redis 패키지가 없거나 연결에 실패하면 None을 반환한다(가용성 우선 원칙)."""
    try:
        import redis
    except ImportError:
        return None
    try:
        client = redis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=2, socket_timeout=5
        )
        client.ping()
    except Exception:
        return None
    return client


def _ensure_seeded(client: Any, settings: Settings) -> None:
    """토큰 리스트가 아직 초기화되지 않았으면 heavy_task_max_concurrency개로 채운다.

    `SET key val NX`가 여러 프로세스가 동시에 첫 호출을 해도 정확히 하나만
    성공하는 것을 보장하므로, 그 하나만 RPUSH로 실제 토큰을 채운다.
    """
    if client.set(_INIT_MARKER_KEY, "1", nx=True) and settings.heavy_task_max_concurrency > 0:
        client.rpush(_TOKEN_KEY, *(["1"] * settings.heavy_task_max_concurrency))


@contextmanager
def heavy_task_slot(
    settings: Settings | None = None,
    *,
    timeout_seconds: float | None = None,
) -> Iterator[None]:
    """Paddle OCR 프로세스 실행·LLM 호출 등 "무거운 작업" 블록을 감싸 동시 실행 개수를 제한한다.

    `heavy_task_max_concurrency`가 0 이하이면 제한 없이 통과시킨다(단일 사용자
    환경·테스트 등). 그 외에는 Redis 토큰을 빌려 블록을 실행하고 끝나면 반납한다.
    타임아웃 안에 토큰을 못 받으면 `HeavyTaskBusyError`를 던져, 호출자가 "지금은
    다른 무거운 작업이 진행 중이니 나중에 다시 시도하라"는 명시적 응답(HTTP 503 등)을
    만들 수 있게 한다 — 사용자에게 아무 신호 없이 요청이 계속 밀리기만 하는 것보다
    낫다는 판단이다.
    """
    active_settings = settings or get_settings()
    if active_settings.heavy_task_max_concurrency <= 0:
        yield
        return

    client = _redis_client(active_settings)
    if client is None:
        # Redis를 못 쓰면 제한 자체를 포기하고 통과시킨다 — 이 세마포어가 없어서
        # OCR/LLM 파이프라인 전체가 막히는 것은 이 최적화의 목적과 어긋난다.
        yield
        return

    _ensure_seeded(client, active_settings)
    wait = (
        active_settings.heavy_task_semaphore_wait_seconds
        if timeout_seconds is None
        else timeout_seconds
    )
    token = client.blpop(_TOKEN_KEY, timeout=wait)
    if token is None:
        raise HeavyTaskBusyError(
            f"{active_settings.heavy_task_max_concurrency}개의 동시 작업 슬롯이 모두 "
            "사용 중입니다. 잠시 후 다시 시도하세요."
        )
    try:
        yield
    finally:
        try:
            client.rpush(_TOKEN_KEY, "1")
        except Exception:
            # 반납 실패는 조용히 넘어간다 — 이미 본 작업은 끝났고, 토큰 유실은
            # 다음 프로세스 재시작 시 _ensure_seeded가 다시 채우기 전까지 남는다.
            pass
