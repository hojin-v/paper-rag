"""검색 서비스 패키지.

자연어 질의를 받아 대표 논문 1편과 연관 논문 1편을 선정하고, 결과를 6시트 엑셀로
내려받을 수 있게 하는 2단계(POST /search → matched|suggest, POST /search/select)
검색 API의 핵심 모듈들을 담는다.

- schemas.py: API 요청/응답 및 내부 결과 번들 스키마 (Pydantic)
- sessions.py: 유사 키워드 제안(suggest) 상태를 담는 세션 저장소
- repository.py: PostgreSQL/pgvector 조회 계층
- service.py: 키워드 매칭·대표/연관 논문 선정 등 핵심 로직
- excel.py: 검색 결과를 6개 시트 엑셀 파일로 직렬화
- api.py: 위 구성요소를 묶는 FastAPI 라우터
"""

