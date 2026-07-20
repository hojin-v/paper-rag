"""PDF 업로드, 레이아웃/OCR 검수, 학습 데이터 내보내기.

이 패키지는 "레이아웃 검출 → 영역별 OCR → 사람/자동 품질 검수 → DB·Vector DB 적재" 로 이어지는
문서 처리 파이프라인 중 사람이 개입하는 검수 구간(review)을 담당한다. 실제 상태 기계와 자동 품질
판정 로직은 `service.py`에, 검수 대상 데이터 모델은 `models.py`에, 파일시스템 저장은 `store.py`에,
관리자용 서버사이드 HTML 뷰어는 `viewer.py`에, REST API 라우팅은 `api.py`에 있다.
"""

from paperrag.review.models import ReviewBlock, ReviewDocument, ReviewPage

__all__ = ["ReviewBlock", "ReviewDocument", "ReviewPage"]
