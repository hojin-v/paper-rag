"""
논문 수집(collect) 서브패키지 — 라이선스가 확인된 논문 PDF를 자동으로 찾아 내려받는다.

전체 수집 파이프라인(STEP 1~8, docs/design/DESIGN.md)의 "입력"에 해당하는 부분으로, 실제 OCR·
LLM 정제·임베딩·DB 적재 이전 단계다. `openalex.py`가 OpenAlex Works API에서 CC 라이선스가
확인된 논문 후보를 찾고, `service.py`(`PaperCollector`)가 실제 PDF를 다운로드·검증·SHA-256
manifest 기록까지 수행한다. `models.py`는 이 둘 사이를 오가는 데이터 구조를, `cli.py`/`__main__.py`
는 `python -m paperrag.collect` 진입점을, `smoke.py`는 CPU에서 빠르게 파이프라인 배선을 확인하기
위한 축소판 PDF 생성기를 담는다. 자세한 배경은 docs/guide/11-paper-collection.md 참고.
"""

from paperrag.collect.models import CollectionReport, DownloadedPaper, PaperCandidate
from paperrag.collect.openalex import OpenAlexClient
from paperrag.collect.service import PaperCollector

__all__ = [
    "CollectionReport",
    "DownloadedPaper",
    "OpenAlexClient",
    "PaperCandidate",
    "PaperCollector",
]
