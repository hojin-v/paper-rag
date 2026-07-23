"""애플리케이션 전역 로깅 설정.

이전에는 `src/paperrag/` 어디에도 `logging` 모듈을 쓰는 곳이 없어(2026-07-23 감사),
운영 중 발생한 에러가 Celery의 기본 트레이스백 출력에만 우연히 남는 상황이었다.
표준 라이브러리 `logging`만으로 stdout에 로그를 남겨(기존 `docker logs` 운영 습관을
그대로 유지) 별도 로그 수집 인프라 없이도 에러를 추적할 수 있게 한다.
"""

from __future__ import annotations

import logging
import sys

from paperrag.config import Settings, get_settings

_configured = False


def configure_logging(settings: Settings | None = None) -> None:
    """루트 로거를 stdout 핸들러 + `Settings.log_level`로 설정한다.

    API/워커/임베더/UI 등 여러 진입점에서 각자 호출해도 두 번째 이후 호출은
    아무 일도 하지 않는다(idempotent) — Streamlit처럼 스크립트가 재실행되는
    환경에서 핸들러가 중복 등록되는 것을 막는다.
    """
    global _configured
    if _configured:
        return
    active_settings = settings or get_settings()
    logging.basicConfig(
        level=active_settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
        force=True,
    )
    _configured = True
