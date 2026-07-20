"""Paper ingestion pipeline package.

논문 수집 파이프라인(STEP 1~8: source check → layout → filter → paragraph →
llm_enrich → keywords → embed → relate)에서 공용으로 쓰는 데이터 모델을
`paperrag.ingest.models`에서 가져와 패키지 최상위로 재노출한다.
실제 각 단계 구현은 `pipeline.py`, `filterer.py`, `paragraphs.py`,
`llm_enrich.py`, `keywords.py`, `repository.py`, `relations.py` 등에 있다.
"""

from paperrag.ingest.models import (
    DocumentLayout,
    EnrichedParagraph,
    IngestReport,
    LayoutBlock,
    PaperMeta,
    ParagraphDraft,
    TableDraft,
)

__all__ = [
    "DocumentLayout",
    "EnrichedParagraph",
    "IngestReport",
    "LayoutBlock",
    "PaperMeta",
    "ParagraphDraft",
    "TableDraft",
]
