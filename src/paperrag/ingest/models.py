from typing import Literal

from pydantic import BaseModel, Field

BlockType = Literal[
    "title",
    "author",
    "abstract",
    "section_header",
    "text",
    "table",
    "table_caption",
    "figure",
    "figure_caption",
    "formula",
    "reference",
    "header_footer",
]

BLOCK_TYPES: set[str] = {
    "title",
    "author",
    "abstract",
    "section_header",
    "text",
    "table",
    "table_caption",
    "figure",
    "figure_caption",
    "formula",
    "reference",
    "header_footer",
}


class LayoutBlock(BaseModel):
    page: int
    block_type: BlockType
    text: str
    order: int
    bbox: tuple[float, float, float, float] | None = None
    confidence: float | None = None
    ocr_engine: str | None = None


class DocumentLayout(BaseModel):
    source_path: str
    is_scanned: bool
    blocks: list[LayoutBlock] = Field(default_factory=list)
    metrics: dict[str, int | float] = Field(default_factory=dict)


class PaperMeta(BaseModel):
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    published_year: int | None = None
    journal: str | None = None
    abstract: str = ""


class ParagraphDraft(BaseModel):
    section_name: str
    paragraph_order: int
    original_text: str


class EnrichedParagraph(BaseModel):
    cleaned_text: str
    summary: str
    keywords: list[str] = Field(default_factory=list)
    is_topic_relevant: bool = True


class TableDraft(BaseModel):
    table_title: str | None = None
    table_text: str


class StageReport(BaseModel):
    status: Literal["pending", "done", "failed"] = "pending"
    count: int = 0
    error: str | None = None


class IngestReport(BaseModel):
    source_path: str
    paper_id: int | None = None
    is_scanned: bool | None = None
    stages: dict[str, StageReport] = Field(default_factory=dict)
    totals: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)

    def record_stage(
        self,
        stage: str,
        *,
        success: bool,
        count: int = 0,
        error: str | None = None,
    ) -> None:
        self.stages[stage] = StageReport(
            status="done" if success else "failed",
            count=count,
            error=error,
        )
        if error:
            self.errors.append(f"{stage}: {error}")

    def set_total(self, name: str, count: int) -> None:
        self.totals[name] = count
