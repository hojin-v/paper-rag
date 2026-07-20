"""내부 API에 대한 최소 접근 통제 — 공유 API 키 기반.

`PAPERRAG_API_KEY`가 설정되지 않으면(기본값, 로컬 개발) 인증을 전혀 검사하지
않는다. 설정돼 있으면 `X-API-Key` 헤더 또는 `api_key` 쿼리 파라미터 중 하나가
그 값과 정확히 일치해야 요청을 통과시킨다.

쿼리 파라미터도 허용하는 이유: 브라우저가 `<img>`/`<iframe>` src로 직접 여는
엔드포인트(레이아웃·OCR 뷰어, 페이지 이미지, 엑셀 다운로드 링크)에는 커스텀
헤더를 실어 보낼 수 없다. 이런 내부 도구에서는 URL에 토큰을 실어 보내는
절충을 택했다(액세스 로그에 남을 수 있다는 점은 감수 — TLS 종단이 있는
배포에서는 로그 자체도 통제되므로 허용 가능한 트레이드오프로 판단했다).

이 모듈은 인증(authentication)만 다룬다 — 역할별 권한(authorization, 예: 관리자
전용 교정과 일반 검색 사용자를 구분하는 것)은 아직 없다. 지금은 조직 내부에서
공유하는 단일 비밀키로 "이 배포에 접근 가능한 사람인지"만 확인하는 최소
버전이며, TLS를 대체하지 않는다(TLS는 리버스 프록시에서 별도로 구성해야 한다).
"""

from fastapi import Depends, Header, HTTPException, Query

from paperrag.config import Settings, get_settings


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    api_key: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """FastAPI 라우트 의존성. `settings.api_key`가 없으면 즉시 통과시킨다.

    설정돼 있는데 헤더·쿼리 어느 쪽도 일치하지 않으면 401을 던진다. `settings`를
    `Depends(get_settings)`로 주입받는 이유는(다른 곳처럼 get_settings()를 직접
    호출하지 않고) 테스트에서 `app.dependency_overrides[get_settings]`로 API 키가
    설정된 상황을 쉽게 재현하기 위함이다.
    """
    if not settings.api_key:
        return
    if x_api_key == settings.api_key or api_key == settings.api_key:
        return
    raise HTTPException(
        status_code=401,
        detail="유효한 API 키가 필요합니다(X-API-Key 헤더 또는 api_key 쿼리 파라미터).",
    )
