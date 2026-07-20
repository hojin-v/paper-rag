"""
BGE-M3 임베딩 HTTP 서버 패키지.

수집 파이프라인(STEP 7 embed)과 검색 서비스가 논문·단락·키워드·표 텍스트를 벡터로 바꿀 때 이
패키지가 노출하는 FastAPI 앱(`server.py`)을 HTTP로 호출한다. 실제 임베딩 계산 로직은
`encoder.py`에 있고, `server.py`는 그 인코더를 얇게 감싼 `/embed`, `/health` 엔드포인트만
제공한다. 별도 프로세스로 띄우는 이유는 무거운 sentence-transformers 모델을 API/워커 프로세스와
분리해, 각 서비스가 독립적으로 스케일·재시작될 수 있게 하기 위함이다(docs/guide/08-embedding.md).
"""

