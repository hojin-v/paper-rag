"""수집 파이프라인 STEP 1~8 전 구간에서 공유하는 데이터 모델.

레이아웃 분석 결과(`LayoutBlock`, `DocumentLayout`), 필터링·단락화 산출물
(`ParagraphDraft`), LLM 정제 결과(`EnrichedParagraph`), 표(`TableDraft`),
논문 메타데이터(`PaperMeta`), 그리고 배치 실행 리포트(`IngestReport`,
`StageReport`)를 정의한다. DESIGN.md §3의 STEP 구성과 §4의 DB 스키마가
이 모델들의 필드와 대응된다.
"""

from typing import Literal

from pydantic import BaseModel, Field

# STEP 2(layout)에서 PP-StructureV3가 분류하는 블록 유형.
# STEP 3(filter)에서 이 유형을 기준으로 메타/본문/표/제외 대상을 나눈다.
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
    """STEP 2(layout)에서 PP-StructureV3가 페이지 단위로 인식한 레이아웃 블록 하나.

    page/order는 읽기 순서 복원 결과이며, bbox·confidence·ocr_engine은 검수 화면과
    품질 모니터링(레이아웃 분류 mAP, OCR CER 등 DESIGN.md §6 지표 산출)에 쓰인다.
    """

    page: int
    block_type: BlockType
    text: str
    order: int
    bbox: tuple[float, float, float, float] | None = None
    confidence: float | None = None
    ocr_engine: str | None = None


class DocumentLayout(BaseModel):
    """STEP 2(layout) 산출물: 문서 전체의 블록 목록과 스캔 여부·품질 지표.

    `is_scanned`는 STEP 1의 문서 성격 판단(triage) 결과를 그대로 담아 이후 단계에서
    참고할 수 있게 하며, `metrics`는 텍스트 커버리지·자동 확장 수 등 레이아웃 보정
    통계를 자유 형식으로 담는다.
    """

    source_path: str
    is_scanned: bool
    blocks: list[LayoutBlock] = Field(default_factory=list)
    metrics: dict[str, int | float] = Field(default_factory=dict)


class PaperMeta(BaseModel):
    """STEP 3(filter)에서 title/author/abstract 블록으로부터 뽑아낸 논문 메타데이터.

    `pipeline.py`의 `_extract_meta`가 이 값을 채우며, `papers` 테이블(DESIGN.md §4)의
    title/authors/published_year/journal/abstract 컬럼과 대응된다. `author_keywords`는
    DB 컬럼과 대응되지 않고 STEP 6(`_score_keywords`)에서 저자가 직접 지정한 키워드
    ("Keywords:"/"CCS Concepts:" 같은 머리말/꼬리말 블록에서 뽑음)를 대표 키워드
    후보에 강제로 포함시키는 데만 쓰인다.
    """

    title: str = ""
    authors: list[str] = Field(default_factory=list)
    published_year: int | None = None
    journal: str | None = None
    abstract: str = ""
    author_keywords: list[str] = Field(default_factory=list)


class ParagraphDraft(BaseModel):
    """STEP 4(paragraph)에서 병합·분할까지 끝낸 단락 원문.

    아직 LLM 정제(STEP 5)를 거치지 않은 `original_text` 상태이며, section_name과
    paragraph_order는 `paragraphs` 테이블에 그대로 저장된다.
    """

    section_name: str
    paragraph_order: int
    original_text: str


class EnrichedParagraph(BaseModel):
    """STEP 5(llm_enrich)에서 단락별 LLM 호출 1회로 생성한 JSON 결과.

    `is_topic_relevant=false`인 단락은 저장은 되지만 검색·엑셀 출력에서는
    제외한다(DESIGN.md §3). LLM 호출이 실패하면 `llm_enrich.PassthroughEnricher`가
    원문을 그대로 채워 이 모델을 만드는 폴백 경로도 있다.
    """

    cleaned_text: str
    summary: str
    keywords: list[str] = Field(default_factory=list)
    is_topic_relevant: bool = True


class TableDraft(BaseModel):
    """STEP 3(filter)에서 골라낸 표 블록을 캡션+본문으로 묶은 표 원본.

    STEP 5에서 `table_summary`가 추가로 생성되어 `paper_tables` 테이블에 함께 저장된다.
    """

    table_title: str | None = None
    table_text: str


class StageReport(BaseModel):
    """`IngestReport.stages`의 값 타입 — STEP 1개 실행 결과(성공/실패/건수/에러)."""

    status: Literal["pending", "done", "failed"] = "pending"
    count: int = 0
    error: str | None = None


class IngestReport(BaseModel):
    """논문 1편에 대한 STEP 1~8 파이프라인 실행 결과를 단계별로 누적하는 리포트.

    `cli.py`가 이 리포트를 모아 콘솔 표와 `docs/reports/ingest/YYYY-MM-DD.md`
    배치 리포트를 만든다.
    """

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
        """한 STEP의 실행 결과를 기록한다. 실패 시 errors 목록에도 추가한다."""
        self.stages[stage] = StageReport(
            status="done" if success else "failed",
            count=count,
            error=error,
        )
        if error:
            self.errors.append(f"{stage}: {error}")

    def set_total(self, name: str, count: int) -> None:
        """단락/키워드/표/연관도 같은 전체 건수 요약값을 기록한다."""
        self.totals[name] = count
