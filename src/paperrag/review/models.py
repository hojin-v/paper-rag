"""검수(review) 파이프라인이 주고받는 Pydantic 데이터 모델.

`review.store.ReviewStore` 구현체(운영은 `PostgresReviewStore`)가 저장하는 `ReviewDocument`가 핵심 엔티티이며,
업로드 시점의 레이아웃 검출 결과부터 OCR·사람 검수·자동 품질 판정·DB 적재까지 문서 하나의 전체
이력을 이 모델 하나에 계속 갱신해 담는다. `ReviewPhase`/`DocumentStatus`가 상태 기계의 단계를,
`ReviewBlock.review_status`가 영역(블록) 단위 검수 상태를 나타낸다. 실제 전이 로직은
`service.ReviewService`에 있고 이 파일은 데이터 형태만 정의한다.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from paperrag.ingest.models import BlockType

# 블록(영역) 단위 검수 상태.
# unreviewed: 아직 사람/자동 판정이 확정되지 않음 (기본값)
# approved:   자동 OCR 결과 그대로 승인됨
# corrected:  사람이 텍스트나 좌표·유형을 직접 교정함
# rejected:   레이아웃 오검출 등으로 이후 OCR·적재 대상에서 제외됨
ReviewStatus = Literal["unreviewed", "approved", "corrected", "rejected"]
# 문서 단위 DB·Vector DB 적재 상태. 검수 단계(phase)와는 별개 축으로,
# ready_to_ingest phase에 도달한 뒤에만 ingesting → ingested/failed로 넘어간다.
DocumentStatus = Literal["analyzed", "ingesting", "ingested", "failed"]
# 문서가 현재 검수 상태 기계의 어느 단계에 있는지.
# layout_review(레이아웃 좌표·유형 검수) → ocr_review(OCR 텍스트 검수)
# → ready_to_ingest(적재 가능). 전이 조건과 각 API의 역할은 service.py 참고.
ReviewPhase = Literal["layout_review", "ocr_review", "ready_to_ingest"]


class AutomationQuality(BaseModel):
    """`ReviewService._automation_quality`가 계산하는 자동 품질 판정 결과.

    사람 승인 없이 진행되는 자동 OCR(`run_automatic_ocr`) 뒤에 이 판정이 "합격(passed)"이어야
    문서가 바로 `ready_to_ingest`로 넘어간다. 실패(needs_review)하면 어떤 기준이 왜 실패했는지
    `reasons`에 사람이 읽을 수 있는 문장으로 남는다.
    """

    status: Literal["passed", "needs_review"]
    eligible_blocks: int  # 품질 판정 대상이 되는 블록 수(표/본문/제목/저자 등 특정 유형만 포함)
    recognized_blocks: int  # eligible_blocks 중 OCR 결과 텍스트가 비어 있지 않은 블록 수
    ocr_coverage: float  # recognized_blocks / eligible_blocks. 이 값이 임계치 미만이면 실패
    title_detected: bool  # 제목 유형 블록이 존재하고 텍스트도 인식되었는지
    author_detected: bool = True  # 저자 유형 블록이 존재하고 텍스트도 인식되었는지
    title_consistent: bool = True  # 제목 OCR 텍스트가 문서 내 인용 메타데이터와 토큰 단위로 일치하는지
    tables_detected: int  # eligible 블록 중 표(table) 유형 개수
    tables_structured: int  # 그중 표 구조화 엔진으로 인식되고 구조화 품질 임계치를 넘긴 개수
    # 자동 품질 실패 시 unreviewed로 되돌려 사람이 다시 볼 블록의 ID 목록.
    # 주의: 현재 구현은 "OCR 결과가 비어 있는 블록"만 이 목록에 담는다. 제목/저자 블록 자체가
    # 레이아웃 단계에서 아예 검출되지 않아 실패한 경우에는 되돌릴 블록이 없어 이 목록이 비고,
    # 문서는 ocr_review phase에 머물지만 관리자가 화면에서 볼 검수 대상 블록은 없는 알려진
    # UX 공백이 있다 (service.py의 run_automatic_ocr/_automation_quality 주석 참고).
    empty_block_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)  # 실패 사유를 사람이 읽을 수 있는 한국어 문장으로 나열


class LayoutQuality(BaseModel):
    """레이아웃 자동 보정(텍스트 검출 좌표 기반 확장·추가·중복 제거) 결과 지표.

    업로드 시 PP-DocLayout 검출 결과와 텍스트 라인 검출 결과를 비교해 얼마나 보정했는지 기록한다.
    검수자가 "자동 레이아웃을 얼마나 신뢰할 수 있는지" 가늠하는 참고 지표이며 검수 통과 여부를
    직접 좌우하지는 않는다(적재 가능 여부는 AutomationQuality가 판단한다).
    """

    detected_text_lines: int = 0  # 텍스트 검출기가 찾은 전체 텍스트 라인 수
    initially_covered_text_lines: int = 0  # 레이아웃 자동 보정 전, 검출 박스에 포함되어 있던 라인 수
    finally_covered_text_lines: int = 0  # 자동 보정(확장·추가) 후 박스에 포함된 라인 수
    initial_text_coverage: float = 0.0  # initially_covered_text_lines / detected_text_lines
    final_text_coverage: float = 0.0  # finally_covered_text_lines / detected_text_lines
    uncovered_text_lines: int = 0  # 보정 후에도 어떤 박스에도 포함되지 않은 라인 수
    expanded_blocks: int = 0  # 잘려 있던 기존 박스를 텍스트 라인에 맞춰 확장한 개수
    added_text_blocks: int = 0  # 박스가 아예 없어 새로 추가한 본문 블록 개수
    split_section_headings: int = 0  # 섹션 제목으로 잘못 합쳐진 박스를 분리한 개수
    recovered_title_blocks: int = 0  # 누락되었던 제목 블록을 텍스트 검출 기반으로 복구한 개수
    recovered_author_blocks: int = 0  # 누락되었던 저자 블록을 텍스트 검출 기반으로 복구한 개수


class ReviewPage(BaseModel):
    """검수 화면에 렌더링된 PDF 한 페이지의 크기·이미지 정보.

    width/height는 PDF 좌표계(pt) 기준이고 image_width/image_height는 실제 렌더링된 PNG
    픽셀 크기다. 블록 bbox는 PDF 좌표계로 저장되므로 학습데이터 export 시 이미지 픽셀 좌표로
    바꾸려면 두 크기의 비율(scale)이 필요하다 (`service._scale_bbox_for_image` 참고).
    """

    page: int
    width: float
    height: float
    image_name: str  # 검수 문서 디렉터리 안의 페이지 이미지 파일명 (예: page-0001.png)
    image_width: int | None = None
    image_height: int | None = None


class ReviewBlock(BaseModel):
    """레이아웃 검출·OCR·사람 검수를 거치는 문서 내 하나의 영역(블록).

    `bbox`/`block_type`은 "현재 유효한" 좌표·유형이고, `detected_bbox`/`detected_block_type`은
    "모델이 처음 검출한 그대로"의 값이다. 관리자가 좌표·유형을 교정해도 detected_* 값은 그대로
    보존되어 화면에서 "자동 검출값 대비 무엇을 고쳤는지" 비교할 수 있게 한다.
    """

    block_id: str
    page: int
    block_type: BlockType  # 현재 유효한(사람이 교정했을 수 있는) 블록 유형
    detected_block_type: BlockType | None = None  # 모델이 처음 검출한 원본 유형(비교용, 사람이 추가한 블록은 None)
    order: int
    bbox: tuple[float, float, float, float] | None = None  # 현재 유효한(사람이 교정했을 수 있는) 좌표
    detected_bbox: tuple[float, float, float, float] | None = None  # 모델이 처음 검출한 원본 좌표(비교용, 사람이 추가한 블록은 None)
    confidence: float | None = None
    ocr_engine: str | None = None
    ocr_text: str = ""  # OCR 모델이 인식한 원문(사람이 교정해도 그대로 보존)
    corrected_text: str | None = None  # 사람이 교정한 텍스트. None이면 아직 교정하지 않은 것
    review_status: ReviewStatus = "unreviewed"

    @property
    def effective_text(self) -> str:
        """실제로 사용할 텍스트. 교정본이 있으면 교정본을, 없으면 OCR 원문을 반환한다.

        적재·학습데이터 export·자동 품질 판정 등 "최종적으로 신뢰할 텍스트"가 필요한 모든 곳에서
        ocr_text 대신 이 속성을 사용해야 사람의 교정이 반영된다.
        """
        if self.corrected_text is not None:
            return self.corrected_text
        return self.ocr_text


class ReviewDocument(BaseModel):
    """검수 파이프라인이 다루는 문서 하나의 전체 상태를 담는 최상위 엔티티.

    업로드 시 생성되어 `ReviewStore`(운영은 `PostgresReviewStore`)에 저장되고, 이후 모든
    검수·자동화 API 호출이 이 객체를 읽고 갱신해 다시 저장하는 방식으로 상태가 이어진다
    (요청 간 상태는 store에만 있다).
    """

    document_id: str
    filename: str
    source_path: str  # 업로드된 원본 PDF의 저장 경로 (document_dir/source.pdf)
    pdf_kind: Literal["digital", "scanned"] | None = None
    processing_mode: Literal["full_ocr"] = "full_ocr"
    backend: str  # 실제로 사용된 레이아웃/OCR 백엔드 (예: "paddle"). upload 시 backend 파라미터로 선택한 값이 확정되어 저장됨
    phase: ReviewPhase = "ready_to_ingest"  # 검수 상태 기계의 현재 단계. 전이 규칙은 service.py 참고
    status: DocumentStatus = "analyzed"  # DB·Vector DB 적재 진행 상태 (phase와 별개)
    pages: list[ReviewPage] = Field(default_factory=list)
    blocks: list[ReviewBlock] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)  # 자동 보정·삭제·품질 실패 등 사람이 알아야 할 이력 메시지 누적
    paper_id: int | None = None  # 적재 완료 후 DB에 생성된 paper 레코드 ID
    error: str | None = None  # 적재 실패 시 예외 메시지
    automation_quality: AutomationQuality | None = None  # 가장 최근 자동 품질 판정 결과
    layout_quality: LayoutQuality | None = None  # 업로드 시점의 레이아웃 자동 보정 지표
    created_at: datetime
    updated_at: datetime


class BlockUpdate(BaseModel):
    """`PUT /documents/{id}/blocks/{block_id}` 요청 바디.

    필드는 전부 선택값이며, 어떤 필드를 바꿀 수 있는지는 문서의 현재 phase에 따라
    `ReviewService.update_block`이 검증한다(좌표·유형은 layout_review 단계에서만,
    corrected_text는 layout_review 단계에서는 불가).
    """

    block_type: BlockType | None = None
    bbox: tuple[float, float, float, float] | None = None
    corrected_text: str | None = None
    review_status: ReviewStatus | None = None


class BlockCreate(BaseModel):
    """`POST /documents/{id}/blocks` 요청 바디. 레이아웃 검수 화면에서 누락 영역을 새로 그렸을 때 전송된다."""

    page: int = Field(ge=1)
    block_type: BlockType
    bbox: tuple[float, float, float, float]


class IngestedDocument(BaseModel):
    """적재(`POST /documents/{id}/ingest`) 성공 응답."""

    document_id: str
    paper_id: int
    status: Literal["ingested"] = "ingested"
    totals: dict[str, int] = Field(default_factory=dict)  # 적재된 논문/단락/요약/키워드 등 개수 통계


class TaskSubmitted(BaseModel):
    """비동기 작업 제출(`POST /documents/{id}/auto-ocr/async` 등) 성공 응답.

    실제 처리 상태는 이 task_id로 `GET /jobs/{task_id}`를 폴링해 확인한다.
    """

    task_id: str


class JobStatus(BaseModel):
    """`GET /jobs/{task_id}` 응답 — Celery 작업의 진행 상태.

    Celery의 PENDING/STARTED/SUCCESS/FAILURE를 그대로 노출한다(더 세분화된 단계별
    진행률은 아직 없다). status="success"면 result에 갱신된 ReviewDocument(JSON)가,
    "failure"면 error에 예외 메시지가 담긴다.
    """

    task_id: str
    status: Literal["pending", "started", "success", "failure"]
    result: dict[str, Any] | None = None
    error: str | None = None
