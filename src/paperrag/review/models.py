from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from paperrag.ingest.models import BlockType

ReviewStatus = Literal["unreviewed", "approved", "corrected", "rejected"]
DocumentStatus = Literal["analyzed", "ingesting", "ingested", "failed"]
ReviewPhase = Literal["layout_review", "ocr_review", "ready_to_ingest"]


class AutomationQuality(BaseModel):
    status: Literal["passed", "needs_review"]
    eligible_blocks: int
    recognized_blocks: int
    ocr_coverage: float
    title_detected: bool
    title_consistent: bool = True
    tables_detected: int
    tables_structured: int
    empty_block_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class LayoutQuality(BaseModel):
    detected_text_lines: int = 0
    initially_covered_text_lines: int = 0
    initial_text_coverage: float = 0.0
    expanded_blocks: int = 0
    added_text_blocks: int = 0


class ReviewPage(BaseModel):
    page: int
    width: float
    height: float
    image_name: str
    image_width: int | None = None
    image_height: int | None = None


class ReviewBlock(BaseModel):
    block_id: str
    page: int
    block_type: BlockType
    detected_block_type: BlockType | None = None
    order: int
    bbox: tuple[float, float, float, float] | None = None
    detected_bbox: tuple[float, float, float, float] | None = None
    confidence: float | None = None
    ocr_engine: str | None = None
    ocr_text: str = ""
    corrected_text: str | None = None
    review_status: ReviewStatus = "unreviewed"

    @property
    def effective_text(self) -> str:
        if self.corrected_text is not None:
            return self.corrected_text
        return self.ocr_text


class ReviewDocument(BaseModel):
    document_id: str
    filename: str
    source_path: str
    pdf_kind: Literal["digital", "scanned"] | None = None
    processing_mode: Literal["full_ocr"] = "full_ocr"
    backend: str
    phase: ReviewPhase = "ready_to_ingest"
    status: DocumentStatus = "analyzed"
    pages: list[ReviewPage] = Field(default_factory=list)
    blocks: list[ReviewBlock] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    paper_id: int | None = None
    error: str | None = None
    automation_quality: AutomationQuality | None = None
    layout_quality: LayoutQuality | None = None
    created_at: datetime
    updated_at: datetime


class BlockUpdate(BaseModel):
    block_type: BlockType | None = None
    bbox: tuple[float, float, float, float] | None = None
    corrected_text: str | None = None
    review_status: ReviewStatus | None = None


class BlockCreate(BaseModel):
    page: int = Field(ge=1)
    block_type: BlockType
    bbox: tuple[float, float, float, float]


class IngestedDocument(BaseModel):
    document_id: str
    paper_id: int
    status: Literal["ingested"] = "ingested"
    totals: dict[str, int] = Field(default_factory=dict)
