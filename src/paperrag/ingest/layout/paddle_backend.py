"""paper-rag의 운영 단일 OCR 경로 — PaddleOCR/PaddleX 기반 레이아웃·OCR·표 구조화 어댑터.

ADR-0002는 원래 "디지털 PDF는 Docling, 스캔 PDF는 PP-StructureV3"라는 이중 트랙을 정했지만,
DESIGN.md §2의 2026-07-12 결정 이후 디지털 파싱 경로는 폐기되고 **모든 PDF(디지털/스캔 구분 없이)를
페이지 이미지로 렌더링한 뒤 이 파일의 경로로만 처리**하는 것이 현재 운영 기준이다
(Docling/SimpleTextLayer는 비교 진단용으로만 남아 있고 운영 적재에는 쓰이지 않는다).

전체 처리 순서는 다음과 같다.

1. `pypdfium2`로 모든 페이지를 이미지로 렌더링한다(`_render_pdf_pages`, DPI는
   `Settings.ocr_render_dpi`).
2. `PP-DocLayout-M`으로 레이아웃(제목/저자/초록/섹션제목/본문/표/그림/참고문헌 등 12클래스,
   DESIGN.md §3 STEP 2)을 검출한다.
3. `PP-OCRv5`(텍스트 검출 `_det` + 한국어 인식 `_rec`)로 레이아웃 박스와 텍스트 검출선을
   대조해 잘린 박스를 확장하고 누락된 본문 영역을 보충한 뒤(`_reconcile_layout_with_text` 등),
   확정된 박스만 crop해 실제 텍스트를 인식한다.
4. 표 영역은 `PP-LCNet_x1_0_table_cls`로 wired/wireless를 분류한 뒤 `SLANeXt_wired` 또는
   `SLANet_plus`로 구조화한다(`_recognize_table`).

이 클래스는 사람이 개입하는 2단계 검수 흐름(`analyze_layout` → 사람 검수 → `recognize_layout`)과,
검수 없이 곧바로 끝까지 실행하는 일괄 처리(`analyze`) 양쪽을 모두 제공한다. 실제 운영에서 어느
메서드가 쓰이는지는 `docs/reports/assessments/2026-07-12-*.md`, `docs/guide/10-production-readiness.md`,
`docs/guide/12-macbook-remote-development-handoff.md`에 실측 근거와 함께 기록돼 있다. 이 파일은
운영 핵심 OCR 경로이므로 수정 시 반드시 실측(레이아웃 mAP·OCR CER·표 TEDS 등, DESIGN.md §6)을
동반해야 한다.
"""

import os
import re
from collections.abc import Iterable, Mapping
from html.parser import HTMLParser
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from typing import Any

from paperrag.config import Settings, get_settings
from paperrag.ingest.layout.dedup import deduplicate_layout_blocks
from paperrag.ingest.models import BlockType, DocumentLayout, LayoutBlock

# PP-StructureV3/PP-DocLayout이 내보내는 다양한 표기(라이브러리 버전·언어에 따라 달라짐)를
# paper-rag 내부 12클래스 BlockType(DESIGN.md §3 STEP 2, ADR-0002 "어노테이션 클래스는 필터링
# 규칙과 동일한 12클래스로 고정")으로 정규화하는 매핑이다. 좌우 값이 여러 개인 것은 동의어
# 흡수용이며(예: "author"/"authors" 모두 "author"), 매핑에 없는 라벨은 기본값 "text"로 떨어진다.
LABEL_MAP: dict[str, BlockType] = {
    "title": "title",
    "doc_title": "title",
    "author": "author",
    "authors": "author",
    "abstract": "abstract",
    "section_header": "section_header",
    "paragraph_title": "section_header",
    "text": "text",
    "paragraph": "text",
    "content": "text",
    "table": "table",
    "table_caption": "table_caption",
    "figure": "figure",
    "image": "figure",
    "figure_caption": "figure_caption",
    "formula": "formula",
    "equation": "formula",
    "reference": "reference",
    "references": "reference",
    "reference_content": "reference",
    "header": "header_footer",
    "footer": "header_footer",
    "page_header": "header_footer",
    "page_footer": "header_footer",
    "number": "header_footer",
    "figure_table_chart_title": "table_caption",
}
# 초록 블록의 첫 줄이 "Abstract"/"초록"/"요약" 같은 헤더 단어로 시작하는지 판정하는 정규식.
# 인라인 초록 분리(`_split_inline_abstract_headings`)와 초록→본문 재분류
# (`_normalize_semantic_block_types`)에서 "이 줄이 초록 헤더인지"를 판단하는 데 공통으로 쓰인다.
ABSTRACT_HEADER_RE = re.compile(r"^\s*(abstract|초록|요약)\b", re.IGNORECASE)


class PaddleBackend:
    """PP-StructureV3/PaddleX 기반 레이아웃·OCR·표 구조화를 공통 LayoutBlock 계약으로 변환한다.

    `LayoutBackend` 프로토콜(`base.py`)의 최소 요구인 `analyze` 외에, 사람 검수를 개입시키는
    2단계 흐름을 위한 `analyze_layout`(레이아웃 검출+보정만 수행)과 `recognize_layout`(확정된
    박스만 crop해 OCR)을 추가로 제공한다. 어떤 메서드를 쓸지는 호출자(`pipeline.py` 배치 수집 vs
    `review/service.py` 검수 흐름)가 결정한다.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        # 표 분류기(PP-LCNet)와 wired/wireless 표 구조화 파이프라인(SLANeXt/SLANet_plus)은
        # 모델 로딩 비용이 커서 최초 사용 시점에 지연 생성하고(`_recognize_table`,
        # `_table_text_for_kind`) 인스턴스 수명 동안 재사용한다.
        self._table_classifier: Any | None = None
        self._table_pipelines: dict[str, Any] = {}

    def analyze(self, pdf_path: str) -> DocumentLayout:
        """PDF 전체를 PP-StructureV3 단일 파이프라인으로 레이아웃 검출+OCR까지 한 번에 끝낸다.

        사람 검수 없이 배치로 끝까지 처리하는 경로(`pipeline.py` STEP 2)에서 사용한다.
        `analyze_layout`+`recognize_layout` 조합과 달리 레이아웃 보정(텍스트 검출선 대조,
        초록/제목 복구 등) 없이 PP-StructureV3가 내놓은 결과를 그대로 매핑하므로, 검수 UI를
        거치지 않는 대신 레이아웃 보정 품질은 더 낮을 수 있다.
        """
        _configure_paddle_runtime(self.settings)
        try:
            from paddleocr import PPStructureV3  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "PaddleOCR가 설치되어 있지 않습니다. `pip install -e \".[ingest-full]\"` 후 "
                "`scripts/download_paddle_models.py`로 사전학습 모델을 준비하세요."
            ) from exc

        # 방향 보정·왜곡 펴기·세로쓰기·인장(seal)·수식·차트·영역 재검출 등 PP-StructureV3의
        # 부가 서브모듈은 전부 비활성화한다. 논문 PDF는 이미 정방향 스캔/디지털 이미지이고
        # 표를 제외한 나머지(수식·차트)는 DESIGN.md §3 STEP 3에서 어차피 필터링 대상이라,
        # 켜봤자 CPU 추론 비용만 늘어나고 실질적인 품질 이득이 없다는 판단이다(실측 근거 문서
        # 없음 — 설계 시점 추정값). 표 구조 인식만 `paddle_use_table_recognition` 설정으로 켤 수
        # 있게 남겨뒀다.
        kwargs: dict[str, Any] = {
            "layout_detection_model_name": self.settings.paddle_layout_model_name,
            "text_detection_model_name": self.settings.paddle_text_detection_model_name,
            "text_recognition_model_name": self.settings.paddle_text_recognition_model_name,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "use_seal_recognition": False,
            "use_table_recognition": self.settings.paddle_use_table_recognition,
            "use_formula_recognition": False,
            "use_chart_recognition": False,
            "use_region_detection": False,
            "device": self.settings.paddle_device,
        }
        # 아래 세 모델 디렉터리(레이아웃/텍스트검출/텍스트인식)는 로컬에 사전 다운로드된 모델을
        # 가리킨다(scripts/download_paddle_models.py, docs/guide/10-production-readiness.md
        # 1단계 표). 설정되어 있으면 온라인 저장소 대신 로컬 캐시를 강제로 사용하도록
        # `_require_model_directory`로 존재를 먼저 검증한다 — 폐쇄망/오프라인 운영 환경에서
        # 조용히 인터넷 다운로드를 시도하다 실패하는 것을 막기 위함이다.
        if self.settings.paddle_layout_model_dir is not None:
            _require_model_directory(
                self.settings.paddle_layout_model_dir,
                "PAPERRAG_PADDLE_LAYOUT_MODEL_DIR",
            )
            kwargs["layout_detection_model_dir"] = str(
                self.settings.paddle_layout_model_dir
            )
        if self.settings.paddle_text_detection_model_dir is not None:
            _require_model_directory(
                self.settings.paddle_text_detection_model_dir,
                "PAPERRAG_PADDLE_TEXT_DETECTION_MODEL_DIR",
            )
            kwargs["text_detection_model_dir"] = str(
                self.settings.paddle_text_detection_model_dir
            )
        if self.settings.paddle_ocr_model_dir is not None:
            _require_model_directory(
                self.settings.paddle_ocr_model_dir,
                "PAPERRAG_PADDLE_OCR_MODEL_DIR",
            )
            kwargs["text_recognition_model_dir"] = str(self.settings.paddle_ocr_model_dir)

        pipeline = PPStructureV3(**kwargs)
        blocks: list[LayoutBlock] = []
        with TemporaryDirectory(prefix="paperrag-ocr-") as temporary_dir:
            # PDF는 텍스트 레이어를 쓰지 않고 항상 페이지 이미지로 렌더링한 뒤 그 이미지에
            # OCR을 돌린다(DESIGN.md STEP 1). DPI는 `Settings.ocr_render_dpi`(기본 200)로,
            # 인식률과 CPU 처리 시간의 절충값이다.
            pages = _render_pdf_pages(
                Path(pdf_path),
                Path(temporary_dir),
                dpi=self.settings.ocr_render_dpi,
            )
            for page_number, image_path, scale in pages:
                for result in pipeline.predict(input=str(image_path)):
                    page_blocks = _map_result(
                        result,
                        page=page_number,
                        start_order=len(blocks),
                    )
                    # 렌더링에 쓴 DPI 배율(scale)만큼 커진 좌표를 원본 PDF 좌표계로 되돌린다.
                    blocks.extend(_scale_blocks(page_blocks, scale))
        return DocumentLayout(
            source_path=str(Path(pdf_path)),
            is_scanned=True,
            blocks=blocks,
        )

    def analyze_layout(self, pdf_path: str) -> DocumentLayout:
        """레이아웃을 검출하고 텍스트 검출 좌표로 누락·잘림을 자동 보정한다.

        검수(review) 흐름의 1단계다. 아직 영역별 OCR 텍스트는 채우지 않고(모든 블록의
        `text=""`), 사람이 검수할 블록 "경계"만 만든다. 실제 텍스트 인식은 사람이 경계를
        확정한 뒤 `recognize_layout`에서 수행한다.

        내부적으로는 (1) PP-DocLayout-M 레이아웃 검출 → (2) 활성화된 경우 PP-OCRv5 텍스트
        검출선과 대조해 레이아웃 박스 확장·누락 본문 추가(`_reconcile_layout_with_text`) →
        (3) 초록/섹션제목이 한 블록에 섞인 경우 분리(`_split_merged_section_regions`,
        `_split_inline_abstract_headings`) → (4) 제목/저자 영역 복구(`_recover_page_title_region`,
        `_recover_author_block_types`) 순서로 페이지별 보정을 적용한다. `metrics`에는 각 보정
        단계의 적용 건수를 담아 반환하며, 이 수치는
        `docs/guide/10-production-readiness.md`·`docs/guide/12-macbook-remote-development-handoff.md`
        에 실측치(예: 2026-07-13 재실측 기준 텍스트 검출선 220개, 초기 커버리지 90%, 자동
        확장 21개·누락 본문 보완 4개)로 기록된 값과 비교하는 데 쓰인다.
        """
        _configure_paddle_runtime(self.settings)
        try:
            from paddlex import create_model  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError("레이아웃 검출에는 PaddleX가 필요합니다.") from exc

        model_dir = self.settings.paddle_layout_model_dir
        if model_dir is None:
            raise FileNotFoundError("PAPERRAG_PADDLE_LAYOUT_MODEL_DIR가 필요합니다.")
        _require_model_directory(model_dir, "PAPERRAG_PADDLE_LAYOUT_MODEL_DIR")
        layout_model = create_model(
            self.settings.paddle_layout_model_name,
            model_dir=str(model_dir),
            device=self.settings.paddle_device,
        )
        text_detector: Any | None = None
        # 인라인 초록/섹션 제목 분리(`_split_inline_abstract_headings`)에서만 필요한 텍스트
        # 인식(recognition) 모델. 후보 줄이 실제로 있을 때만 지연 생성한다(아래 루프 참고).
        heading_recognizer: Any | None = None
        # `paddle_layout_text_reconcile`이 꺼져 있으면 레이아웃 모델 원시 출력만 쓰고 아래
        # 텍스트 검출 기반 보정(확장·누락 보완·분리·복구) 전부를 건너뛴다.
        if self.settings.paddle_layout_text_reconcile:
            detector_dir = self.settings.paddle_text_detection_model_dir
            if detector_dir is None:
                raise FileNotFoundError(
                    "PAPERRAG_PADDLE_TEXT_DETECTION_MODEL_DIR가 필요합니다."
                )
            _require_model_directory(
                detector_dir,
                "PAPERRAG_PADDLE_TEXT_DETECTION_MODEL_DIR",
            )
            text_detector = create_model(
                self.settings.paddle_text_detection_model_name,
                model_dir=str(detector_dir),
                device=self.settings.paddle_device,
            )
        blocks: list[LayoutBlock] = []
        # 문서 전체(모든 페이지 합산) 보정 통계. 각 항목은 이후 페이지 루프에서 페이지별
        # `page_metrics`를 누적한 값이며, 반환 시 커버리지 비율(coverage) 계산에도 쓰인다.
        # 이 수치는 레이아웃 자동 보정이 실제로 얼마나 개입했는지(=원본 모델 출력을 얼마나
        # 신뢰할 수 없어 보정에 의존했는지)를 드러내는 관측값이다.
        metric_totals: dict[str, int] = {
            "detected_text_lines": 0,
            "initially_covered_text_lines": 0,
            "finally_covered_text_lines": 0,
            "expanded_blocks": 0,
            "added_text_blocks": 0,
            "split_section_headings": 0,
            "recovered_title_blocks": 0,
            "recovered_author_blocks": 0,
        }
        with TemporaryDirectory(prefix="paperrag-layout-") as temporary_dir:
            pages = _render_pdf_pages(
                Path(pdf_path),
                Path(temporary_dir),
                dpi=self.settings.ocr_render_dpi,
            )
            for page_number, image_path, scale in pages:
                page_blocks: list[LayoutBlock] = []
                for result in layout_model.predict(
                    str(image_path),
                    # 레이아웃 분류 confidence 하한(기본 0.3). 이보다 낮은 신뢰도의 박스는
                    # 애초에 후보에서 제외해 오탐(false positive)을 억제한다
                    # (`Settings.paddle_layout_threshold`, 실측 근거 문서 없음 — 추정값).
                    threshold=self.settings.paddle_layout_threshold,
                ):
                    page_blocks.extend(
                        _map_layout_detection_result(
                            result,
                            page=page_number,
                            start_order=len(blocks) + len(page_blocks),
                        )
                    )
                if text_detector is not None:
                    # 이 페이지에서 PP-OCRv5 텍스트 검출기가 찾은 모든 "글자가 있는 줄"의
                    # bbox+score. 레이아웃 모델이 놓친 영역을 찾아내는 기준선(ground truth에
                    # 가까운 근사치)으로 쓰인다 — 레이아웃 박스 자체가 부정확할 수 있는 것과
                    # 달리, 텍스트 검출은 "글자가 있다/없다"만 판단하므로 상대적으로 안정적이다.
                    text_lines: list[
                        tuple[tuple[float, float, float, float], float | None]
                    ] = []
                    for result in text_detector.predict(str(image_path)):
                        text_lines.extend(_map_text_detection_result(result))
                    # 1단계 보정: 레이아웃 박스와 텍스트 검출선을 대조해 (a) 텍스트 줄이 박스
                    # 경계 밖으로 삐져나온 "잘린 박스"를 확장하고 (b) 어떤 레이아웃 박스에도
                    # 속하지 않는 텍스트 줄 뭉치를 새 본문(text) 블록으로 추가한다. 2026-07-13
                    # 실측(docs/guide/12-macbook-remote-development-handoff.md §5): 텍스트
                    # 검출선 220개 중 초기 레이아웃 커버리지 90%, 21개 박스 확장·4개 본문 블록
                    # 추가로 91.7%까지 개선.
                    page_blocks, page_metrics = _reconcile_layout_with_text(
                        page_blocks,
                        text_lines,
                        page=page_number,
                        start_order=len(blocks),
                        coverage_threshold=self.settings.paddle_text_coverage_threshold,
                        merge_gap_ratio=self.settings.paddle_text_merge_gap_ratio,
                        abstract_merge_gap_ratio=(
                            self.settings.paddle_abstract_merge_gap_ratio
                        ),
                        abstract_merge_x_overlap=(
                            self.settings.paddle_abstract_merge_x_overlap
                        ),
                    )
                    # 2단계 보정: 레이아웃 모델이 "섹션 제목 한 줄 + 본문 여러 줄"을 하나의
                    # abstract/section_header 블록으로 뭉쳐 인식한 경우, 첫 줄만 제목으로 분리하고
                    # 나머지를 본문(초록이면 abstract 유지, 섹션 제목이면 text)으로 되돌린다.
                    page_blocks, split_count = _split_merged_section_regions(
                        page_blocks,
                        text_lines,
                        page=page_number,
                        start_order=len(blocks),
                        enabled=self.settings.paddle_section_heading_split,
                        min_body_lines=(
                            self.settings.paddle_section_heading_min_body_lines
                        ),
                        max_heading_width_ratio=(
                            self.settings.paddle_section_heading_max_width_ratio
                        ),
                        line_overlap=self.settings.paddle_section_heading_line_overlap,
                    )
                    # 3단계 보정 후보: 위 두 단계로도 분리되지 않은, "제목과 본문이 같은 줄에
                    # 붙어 있는"(예: "Abstract This paper...") 인라인 패턴의 첫 줄 bbox만 모은다.
                    # 실제로 헤더 단어인지는 아직 모르므로(텍스트를 인식하지 않았음) 여기서는
                    # 순수 기하 조건(최소 본문 줄 수)만으로 후보를 추린다.
                    inline_candidates = _inline_abstract_candidate_bboxes(
                        page_blocks,
                        text_lines,
                        min_body_lines=(
                            self.settings.paddle_section_heading_min_body_lines
                        ),
                        line_overlap=self.settings.paddle_section_heading_line_overlap,
                        include_text_blocks=page_number == 1,
                    )
                    if (
                        self.settings.paddle_inline_abstract_split
                        and inline_candidates
                    ):
                        # 후보가 있을 때만 텍스트 인식(recognition) 모델을 지연 생성한다 —
                        # 대부분의 페이지에는 인라인 패턴이 없으므로 불필요한 모델 로딩을 피한다.
                        if heading_recognizer is None:
                            recognizer_dir = self.settings.paddle_ocr_model_dir
                            if recognizer_dir is None:
                                raise FileNotFoundError(
                                    "PAPERRAG_PADDLE_OCR_MODEL_DIR가 필요합니다."
                                )
                            _require_model_directory(
                                recognizer_dir,
                                "PAPERRAG_PADDLE_OCR_MODEL_DIR",
                            )
                            heading_recognizer = create_model(
                                self.settings.paddle_text_recognition_model_name,
                                model_dir=str(recognizer_dir),
                                device=self.settings.paddle_device,
                            )
                        # 후보 줄만 실제로 crop해 OCR 인식을 수행한다(레이아웃 박스 전체가
                        # 아니라 후보 줄 bbox만 crop하므로 비용이 작다). 인식 결과가
                        # `ABSTRACT_HEADER_RE`("Abstract"/"초록"/"요약")와 일치해야 다음
                        # 단계에서 실제 분리를 수행한다.
                        recognized_headings = _recognize_heading_candidates(
                            heading_recognizer,
                            image_path,
                            inline_candidates,
                            output_dir=Path(temporary_dir),
                            page=page_number,
                            min_confidence=(
                                self.settings.paddle_inline_heading_ocr_min_confidence
                            ),
                        )
                        page_blocks, inline_split_count = (
                            _split_inline_abstract_headings(
                                page_blocks,
                                text_lines,
                                recognized_headings,
                                page=page_number,
                                start_order=len(blocks),
                                min_body_lines=(
                                    self.settings.paddle_section_heading_min_body_lines
                                ),
                                line_overlap=(
                                    self.settings.paddle_section_heading_line_overlap
                                ),
                                max_prefix_ratio=(
                                    self.settings.paddle_inline_heading_max_prefix_ratio
                                ),
                                include_text_blocks=page_number == 1,
                            )
                        )
                        split_count += inline_split_count
                    # 4단계 보정: 1페이지에서 title 블록이 하나도 없으면, 초록/섹션제목보다
                    # 위쪽에 있고 충분히 넓은(페이지 폭 대비 `paddle_title_min_width_ratio`
                    # 이상) text 블록을 제목으로 재분류한다. LayoutLMv2 실측에서 제목 검출이
                    # 자주 누락된 문제(docs/guide/12 §5~6)에 대한 보완책이다.
                    page_blocks, recovered_titles = _recover_page_title_region(
                        page_blocks,
                        text_lines,
                        page=page_number,
                        enabled=self.settings.paddle_title_region_recovery,
                        min_width_ratio=self.settings.paddle_title_min_width_ratio,
                    )
                    # 5단계 보정: 제목과 초록/섹션제목 사이에 낀 text 블록을 저자(author)로
                    # 재분류한다(`_probable_author_region_orders`). 저자 줄이 레이아웃 모델에서
                    # `author`가 아닌 일반 `text`로 분류되는 실제 사례가 있어(docs/guide/12 §6)
                    # 저자 메타데이터 결측을 줄이기 위한 순수 위치 기반 휴리스틱이다.
                    page_blocks, recovered_authors = _recover_author_block_types(
                        page_blocks,
                        enabled=self.settings.paddle_author_region_recovery,
                    )
                    page_metrics["split_section_headings"] = split_count
                    page_metrics["recovered_title_blocks"] = recovered_titles
                    page_metrics["recovered_author_blocks"] = recovered_authors
                    for key in metric_totals:
                        metric_totals[key] += int(page_metrics[key])
                blocks.extend(_scale_blocks(page_blocks, scale))
        detected_lines = metric_totals["detected_text_lines"]
        # 커버리지 = "레이아웃 박스에 포함된 텍스트 검출선 수 / 전체 텍스트 검출선 수".
        # 1단계 보정(_reconcile_layout_with_text) 전후 값을 나란히 남겨, 자동 보정이 실제로
        # 커버리지를 얼마나 끌어올렸는지 검수 화면·리포트에서 확인할 수 있게 한다.
        metrics: dict[str, int | float] = {
            **metric_totals,
            "initial_text_coverage": (
                metric_totals["initially_covered_text_lines"] / detected_lines
                if detected_lines
                else 0.0
            ),
            "final_text_coverage": (
                metric_totals["finally_covered_text_lines"] / detected_lines
                if detected_lines
                else 0.0
            ),
            "uncovered_text_lines": (
                detected_lines - metric_totals["finally_covered_text_lines"]
            ),
        }
        return DocumentLayout(
            source_path=str(Path(pdf_path)),
            is_scanned=True,
            blocks=blocks,
            metrics=metrics,
        )

    def recognize_layout(
        self,
        pdf_path: str,
        reviewed_blocks: list[LayoutBlock],
    ) -> DocumentLayout:
        """확정된 레이아웃 박스를 crop한 뒤 일반 OCR 또는 표 OCR을 실행한다.

        검수(review) 흐름의 2단계다. `reviewed_blocks`는 사람이 `analyze_layout` 결과를
        보고 추가·삭제·좌표 수정까지 마친 최종 블록 경계이며, 이 메서드는 그 경계를
        신뢰하고 각 영역을 실제로 crop해 텍스트를 채운다. figure/formula 블록은 어차피
        STEP 3(filter)에서 제외되므로 OCR을 건너뛰고 그대로 통과시킨다.
        """
        _configure_paddle_runtime(self.settings)
        try:
            from paddleocr import PaddleOCR  # type: ignore[import-not-found]
            from PIL import Image
        except ImportError as exc:
            raise ImportError("영역별 OCR에는 PaddleOCR와 Pillow가 필요합니다.") from exc

        ocr = PaddleOCR(
            text_detection_model_name=self.settings.paddle_text_detection_model_name,
            text_detection_model_dir=str(self.settings.paddle_text_detection_model_dir),
            text_recognition_model_name=self.settings.paddle_text_recognition_model_name,
            text_recognition_model_dir=str(self.settings.paddle_ocr_model_dir),
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            device=self.settings.paddle_device,
        )
        recognized: list[LayoutBlock] = []
        blocks_by_page: dict[int, list[LayoutBlock]] = {}
        for block in reviewed_blocks:
            blocks_by_page.setdefault(block.page, []).append(block)

        with TemporaryDirectory(prefix="paperrag-region-ocr-") as temporary_dir:
            temporary_path = Path(temporary_dir)
            pages = _render_pdf_pages(
                Path(pdf_path),
                temporary_path,
                dpi=self.settings.ocr_render_dpi,
            )
            for page_number, image_path, scale in pages:
                with Image.open(image_path) as page_image:
                    for block in sorted(
                        blocks_by_page.get(page_number, []), key=lambda item: item.order
                    ):
                        if block.bbox is None or block.block_type in {"figure", "formula"}:
                            recognized.append(block)
                            continue
                        crop_box = _scaled_crop_box(block.bbox, scale, page_image.size)
                        crop_path = temporary_path / f"ocr-{page_number}-{block.order}.png"
                        page_image.crop(crop_box).save(crop_path)
                        text, confidence, engine = self._recognize_crop(
                            crop_path,
                            block.block_type,
                            ocr,
                        )
                        recognized.append(
                            block.model_copy(
                                update={
                                    "text": text,
                                    # 검수 화면에서 사람이 이미 confidence를 부여했다면(예: 수동
                                    # 추가 박스) 그 값을 유지하고, 없을 때만 새 OCR confidence로
                                    # 채운다 — 사람 판단이 자동 추정값보다 우선한다.
                                    "confidence": block.confidence
                                    if block.confidence is not None
                                    else confidence,
                                    "ocr_engine": engine,
                                }
                            )
                        )
        recognized.sort(key=lambda item: item.order)
        # 사람이 표시한 block_type을 신뢰하되, 초록 뒤에 이어지는 섹션이 시작된 뒤에도
        # abstract로 남아 있는 등 명백히 어긋나는 경우만 최소한으로 재정규화한다
        # (`_normalize_semantic_block_types` 참고).
        recognized = _normalize_semantic_block_types(
            recognized,
            recover_author_region=self.settings.paddle_author_region_recovery,
        )
        return DocumentLayout(
            source_path=str(Path(pdf_path)),
            is_scanned=True,
            blocks=recognized,
        )

    def _recognize_crop(
        self,
        crop_path: Path,
        block_type: BlockType,
        ocr: Any,
    ) -> tuple[str, float | None, str]:
        """crop 이미지 한 장을 OCR한다. block_type이 table이면 표 구조화를 우선 시도한다."""
        if block_type == "table" and self.settings.paddle_use_table_recognition:
            table_text, table_kind = self._recognize_table(crop_path)
            if table_text:
                return table_text, None, f"paddle-table-structure-{table_kind}"
        results = ocr.predict(str(crop_path))
        if not results:
            return "", None, "pp-ocrv5-region"
        payload = _as_mapping(results[0])
        texts = payload.get("rec_texts")
        scores = payload.get("rec_scores")
        text = "\n".join(str(value).strip() for value in texts or [] if str(value).strip())
        confidence_values = [float(value) for value in scores or []]
        confidence = (
            sum(confidence_values) / len(confidence_values) if confidence_values else None
        )
        engine = "pp-ocrv5-table-coordinate" if block_type == "table" else "pp-ocrv5-region"
        return text, confidence, engine

    def _recognize_table(self, crop_path: Path) -> tuple[str, str | None]:
        """표 영역 하나를 wired/wireless로 분류한 뒤 구조화 결과가 나쁘면 반대 모델도 시도한다.

        표는 선(wired, 예: 격자 표)이 있는지 없는지(wireless, 예: 여백만으로 구분된 표)에 따라
        구조 인식 모델이 다르다(PP-LCNet 분류 → SLANeXt_wired 또는 SLANet_plus). 분류가 DPI나
        스캔 품질에 따라 흔들릴 수 있다는 잔여 위험이 실측으로 확인되어 있어
        (docs/guide/10-production-readiness.md 4단계 "wired/무선 분류가 DPI에 따라 바뀜"),
        1차 분류 결과의 구조화 품질(`_table_structure_quality`, TEDS를 대체하는 임시 유사
        지표 — 실제 TEDS 정답 평가는 아직 없음)이 임계값(`paddle_table_min_structure_quality`,
        기본 0.7, 실측 근거 문서 없음 — 추정값) 미만이면 반대 모델도 실행해 더 나은 쪽을
        채택한다. 두 모델 디렉터리 중 하나라도 준비되지 않았으면 빈 문자열을 반환해 호출자가
        일반 OCR로 폴백하게 한다.
        """
        _configure_paddle_runtime(self.settings)
        if self._table_classifier is None:
            classifier_dir = self.settings.paddle_table_classification_model_dir
            if classifier_dir is None or not classifier_dir.is_dir():
                return "", None
            try:
                from paddlex import create_model  # type: ignore[import-not-found]
            except ImportError:
                return "", None
            self._table_classifier = create_model(
                "PP-LCNet_x1_0_table_cls",
                model_dir=str(classifier_dir),
                device=self.settings.paddle_device,
            )
        classifications = list(self._table_classifier.predict(str(crop_path)))
        if not classifications:
            return "", None
        table_kind, _ = _best_table_classification(_as_mapping(classifications[0]))
        if table_kind not in {"wired", "wireless"}:
            return "", None
        primary_text = self._table_text_for_kind(crop_path, table_kind)
        primary_quality = _table_structure_quality(primary_text)
        if primary_quality >= self.settings.paddle_table_min_structure_quality:
            return primary_text, table_kind
        # 1차 분류 결과의 구조화 품질이 낮으면 반대 종류(wired↔wireless) 모델로도 한 번 더
        # 돌려보고, 그쪽이 더 낫다면 그 결과를 채택한다. 그래도 더 낫지 않으면 원래 분류 결과를
        # 그대로 반환한다(둘 다 나쁘더라도 완전히 버리지 않고 최선의 추정치를 남긴다).
        alternate_kind = "wireless" if table_kind == "wired" else "wired"
        alternate_text = self._table_text_for_kind(crop_path, alternate_kind)
        if _table_structure_quality(alternate_text) > primary_quality:
            return alternate_text, alternate_kind
        return primary_text, table_kind

    def _table_text_for_kind(self, crop_path: Path, table_kind: str) -> str:
        """지정된 종류(wired/wireless)의 표 구조화 파이프라인으로 crop 이미지를 처리한다."""
        try:
            from paddlex import create_pipeline  # type: ignore[import-not-found]
            from paddlex.inference.pipelines import load_pipeline_config
        except ImportError:
            return ""
        model_name = "SLANeXt_wired" if table_kind == "wired" else "SLANet_plus"
        table_model_dir = (
            self.settings.paddle_wired_table_model_dir
            if table_kind == "wired"
            else self.settings.paddle_wireless_table_model_dir
        )
        if table_model_dir is None or not table_model_dir.is_dir():
            return ""
        # wired/wireless 파이프라인은 종류별로 한 번만 구성해 캐시한다(모델 로딩 비용).
        pipeline = self._table_pipelines.get(table_kind)
        if pipeline is None:
            # PaddleX의 범용 "table_recognition" 파이프라인 설정을 가져와, 이미 앞 단계에서
            # 끝낸 문서 전처리·레이아웃 검출은 다시 하지 않도록 끄고(`use_doc_preprocessor`,
            # `use_layout_detection`는 False), 표 구조 인식 모델만 wired/wireless에 맞는
            # 모델(SLANeXt_wired/SLANet_plus)로 갈아 끼운다. 표 안 텍스트 인식(OCR)은
            # 레이아웃/전체 OCR 경로와 동일한 텍스트 검출·인식 모델(PP-OCRv5, 한국어 인식)을
            # 재사용해 표 안팎의 인식 품질·엔진을 일관되게 유지한다.
            config = load_pipeline_config("table_recognition")
            config["use_doc_preprocessor"] = False
            config["use_layout_detection"] = False
            config["use_ocr_model"] = True
            structure = config["SubModules"]["TableStructureRecognition"]
            structure["model_name"] = model_name
            structure["model_dir"] = str(table_model_dir)
            ocr_modules = config["SubPipelines"]["GeneralOCR"]["SubModules"]
            ocr_modules["TextDetection"]["model_name"] = (
                self.settings.paddle_text_detection_model_name
            )
            ocr_modules["TextDetection"]["model_dir"] = str(
                self.settings.paddle_text_detection_model_dir
            )
            ocr_modules["TextRecognition"]["model_name"] = (
                self.settings.paddle_text_recognition_model_name
            )
            ocr_modules["TextRecognition"]["model_dir"] = str(
                self.settings.paddle_ocr_model_dir
            )
            pipeline = create_pipeline(config=config, device=self.settings.paddle_device)
            self._table_pipelines[table_kind] = pipeline
        results = list(
            pipeline.predict(
                str(crop_path),
                use_layout_detection=False,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_ocr_model=True,
            )
        )
        if not results:
            return ""
        payload = _as_mapping(results[0])
        table_rows = payload.get("table_res_list")
        if not isinstance(table_rows, list) or not table_rows:
            return ""
        first = table_rows[0]
        if not isinstance(first, Mapping):
            return ""
        # SLANeXt/SLANet_plus는 표 구조를 HTML(`pred_html`, 병합 셀은 colspan/rowspan 포함)로
        # 내놓는다. Excel 등 평면 텍스트 출력에 맞춰 `_html_table_to_pipe_text`로 "|"-구분
        # 텍스트로 정규화한다. 병합 셀의 정확한 구조 복원은 보장하지 않는다
        # (docs/reports/assessments/2026-07-12-production-readiness.md 2단계).
        return _html_table_to_pipe_text(str(first.get("pred_html") or ""))


def _map_result(result: Any, *, page: int, start_order: int = 0) -> list[LayoutBlock]:
    """PP-StructureV3 단일 파이프라인(`analyze`)의 페이지 1개 결과를 LayoutBlock 목록으로 변환한다.

    PP-StructureV3는 레이아웃 검출과 OCR을 한 번에 수행하므로, 여기서는 결과 payload에서
    블록 후보를 찾아(`_find_block_candidates`) 라벨→BlockType 매핑, 텍스트·bbox 추출까지
    한 번에 끝낸다. `analyze_layout`/`recognize_layout` 경로와 달리 텍스트 검출선 대조 보정은
    적용되지 않는다.
    """
    payload = _as_mapping(result)
    candidates = _find_block_candidates(payload)
    blocks: list[LayoutBlock] = []
    for candidate in candidates:
        label = _first_text(candidate, "block_label", "label", "type", "category")
        block_type = LABEL_MAP.get(_normalize_label(label), "text")
        text = _first_text(
            candidate,
            "block_content",
            "content",
            "text",
            "rec_text",
            "markdown",
        )
        bbox = _bbox(candidate)
        # PP-StructureV3가 표 영역의 markdown/content를 못 채운 경우, 전체 OCR 결과에서 해당
        # bbox 안에 있는 토큰들을 좌표로 행·열 정렬해 텍스트라도 보존한다(정밀 표 구조화가 아닌
        # "표 안 글자를 잃어버리지 않기" 위한 대체 경로).
        if not text and block_type == "table" and bbox is not None:
            text = _ocr_text_in_bbox(payload, bbox)
        # figure/formula는 어차피 STEP 3(filter)에서 제외되므로 텍스트가 비어도 블록 자체는
        # 유지한다(위치 정보 보존용). 그 외 유형은 텍스트가 없으면 노이즈로 보고 버린다.
        if not text and block_type not in {"figure", "formula"}:
            continue
        blocks.append(
            LayoutBlock(
                page=_page_number(candidate, page),
                block_type=block_type,
                text=text,
                order=start_order + len(blocks),
                bbox=bbox,
                # 블록 자체의 신뢰도 점수가 없으면(라이브러리 버전에 따라 생략되기도 함)
                # 같은 라벨·유사 bbox를 가진 레이아웃 검출 결과의 점수로 대체 추정한다.
                confidence=_score(candidate) or _layout_score(payload, label, bbox),
                ocr_engine="pp-structurev3",
            )
        )
    # PP-StructureV3가 어떤 레이아웃 블록에도 배정하지 않은 전체 OCR 텍스트 조각이 있을 수
    # 있다(예: 그림 안 캡션이 아닌 산발적 텍스트). 기존 레이아웃 블록과 겹치지 않는 것만 골라
    # 별도 블록으로 만들고, 읽기 순서상 적절한 위치에 끼워 넣는다.
    fallback_blocks = _unassigned_ocr_blocks(
        payload,
        blocks,
        page=page,
        start_order=start_order + len(blocks),
    )
    if not fallback_blocks:
        return blocks
    return _merge_in_reading_order(blocks, fallback_blocks, start_order=start_order)


def _map_layout_detection_result(
    result: Any,
    *,
    page: int,
    start_order: int = 0,
) -> list[LayoutBlock]:
    """`analyze_layout` 경로에서 레이아웃 검출 모델(PP-DocLayout-M) 원시 결과 1페이지분을
    LayoutBlock 목록으로 변환한다. 이 시점에는 아직 OCR 텍스트가 없다(`text=""`).
    """
    payload = _as_mapping(result)
    rows = payload.get("boxes")
    if not isinstance(rows, list):
        nested = payload.get("layout_det_res")
        rows = nested.get("boxes") if isinstance(nested, Mapping) else None
    if not isinstance(rows, list):
        return []
    blocks: list[LayoutBlock] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        label = _first_text(row, "label", "block_label", "type", "category")
        bbox = _bbox(row)
        if bbox is None:
            continue
        blocks.append(
            LayoutBlock(
                page=page,
                block_type=LABEL_MAP.get(_normalize_label(label), "text"),
                text="",
                order=start_order + len(blocks),
                bbox=bbox,
                confidence=_score(row),
                ocr_engine=None,
            )
        )
    # 같은 영역을 가리키는 중복 박스를 제거한 뒤(`deduplicate_layout_blocks`), 열 구조를
    # 고려한 읽기 순서로 정렬한다(`_order_detected_blocks`) — 좌표만으로는 원소 순서가
    # 논문의 실제 읽기 순서(2단 조판의 좌→우 열 순서 등)와 다를 수 있기 때문이다.
    return _order_detected_blocks(
        deduplicate_layout_blocks(blocks),
        start_order=start_order,
    )


def _map_text_detection_result(
    result: Any,
) -> list[tuple[tuple[float, float, float, float], float | None]]:
    """PP-OCRv5 텍스트 검출(det) 결과 1페이지분을 (bbox, score) 목록으로 변환한다.

    PaddleX 텍스트 검출은 글자 줄을 다각형(polygon, `dt_polys`)으로 반환하므로, 레이아웃
    보정 로직(사각형 bbox 기반)과 맞추기 위해 다각형의 외접 사각형(min/max)으로 단순화한다.
    """
    payload = _as_mapping(result)
    polygons = payload.get("dt_polys")
    scores = payload.get("dt_scores")
    if hasattr(polygons, "tolist"):
        polygons = polygons.tolist()
    if hasattr(scores, "tolist"):
        scores = scores.tolist()
    if not isinstance(polygons, list):
        return []
    mapped: list[tuple[tuple[float, float, float, float], float | None]] = []
    for index, polygon in enumerate(polygons):
        if not isinstance(polygon, (list, tuple)):
            continue
        points = [
            point
            for point in polygon
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]
        if not points:
            continue
        try:
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
            score = (
                float(scores[index])
                if isinstance(scores, list) and index < len(scores)
                else None
            )
        except (TypeError, ValueError):
            continue
        mapped.append(((min(xs), min(ys), max(xs), max(ys)), score))
    return mapped


def _reconcile_layout_with_text(
    blocks: list[LayoutBlock],
    text_lines: list[tuple[tuple[float, float, float, float], float | None]],
    *,
    page: int,
    start_order: int,
    coverage_threshold: float,
    merge_gap_ratio: float,
    abstract_merge_gap_ratio: float = 0.5,
    abstract_merge_x_overlap: float = 0.7,
) -> tuple[list[LayoutBlock], dict[str, int]]:
    """레이아웃 박스를 텍스트 검출선(det) 좌표와 대조해 잘린 박스를 확장하고 누락 영역을 추가한다.

    `analyze_layout`의 1단계 보정으로, `PaddleBackend.analyze_layout` docstring에 실측
    수치(2026-07-13, 텍스트 검출선 220개·초기 커버리지 90%·확장 21개·본문 추가 4개)가
    기록돼 있다. 반환하는 두 번째 값(`dict`)은 이 페이지의 보정 통계이며 호출자가
    `metric_totals`에 누적한다.
    """
    updated = list(blocks)
    # 텍스트 줄을 흡수해 경계를 넓혀도 되는(=원래도 "글 상자"였던) 블록 유형만 확장 대상으로
    # 삼는다. figure/table/formula는 텍스트 검출선이 우연히 겹치더라도 그림·수식·표 내부
    # 캡션이 아닌 그림 자체를 침범하지 않도록 제외한다.
    expandable_types = {
        "title",
        "author",
        "abstract",
        "section_header",
        "text",
        "table_caption",
        "figure_caption",
        "reference",
        "header_footer",
    }
    initially_covered = 0
    expanded_indices: set[int] = set()
    unassigned: list[tuple[tuple[float, float, float, float], float | None]] = []
    for line_bbox, line_score in text_lines:
        # "커버리지"는 텍스트 줄 면적 기준 교집합 비율(교집합 넓이 / 줄 넓이)이다. 줄이 여러
        # 블록에 걸쳐 있으면 가장 많이 겹치는 블록을 대표로 택한다.
        overlaps = [
            _bbox_intersection_fraction(line_bbox, block.bbox)
            for block in updated
        ]
        best_index = max(range(len(overlaps)), key=overlaps.__getitem__) if overlaps else None
        best_overlap = overlaps[best_index] if best_index is not None else 0.0
        if best_overlap >= coverage_threshold:
            initially_covered += 1
        if best_index is not None:
            block = updated[best_index]
            vertical_overlap = _bbox_vertical_overlap_fraction(line_bbox, block.bbox)
            # 완전히 커버되지 않았더라도(best_overlap > 0) 세로 방향으로 절반 이상
            # 겹치면(vertical_overlap >= 0.5, 실측 근거 문서 없음 — 추정값) "이 줄은 원래 이
            # 블록에 속하는데 레이아웃 박스가 좌우로 짧게 잘렸다"고 보고 박스를 텍스트 줄까지
            # 확장한다(합집합). 완전히 벗어난 줄(수평으로 겹치지 않는 줄)은 여기서 걸러지지
            # 않고 아래 unassigned 후보로 넘어간다.
            if (
                best_overlap > 0.0
                and vertical_overlap >= 0.5
                and block.block_type in expandable_types
                and block.bbox is not None
            ):
                union = _bbox_union(block.bbox, line_bbox)
                if union != block.bbox:
                    updated[best_index] = block.model_copy(update={"bbox": union})
                    expanded_indices.add(best_index)
                continue
        # 어떤 블록과도 충분히 겹치지 않는(coverage_threshold 미만, 기본 0.8) 텍스트 줄은
        # "레이아웃 모델이 아예 놓친 영역"으로 보고 별도 그룹핑 대상으로 남긴다.
        if best_overlap < coverage_threshold:
            unassigned.append((line_bbox, line_score))

    # 배정되지 않은 텍스트 줄들을 인접한 것끼리 묶어 "새 블록 후보" 그룹으로 만든다(줄 단위가
    # 아니라 문단 단위로 묶어야 이후 처리에서 다루기 쉽다).
    fallback_groups = _merge_unassigned_text_lines(
        unassigned,
        gap_ratio=merge_gap_ratio,
    )
    # 초록 병합 시 "인접하다"의 기준 간격을 절대 픽셀이 아니라 이 페이지의 전형적인 줄
    # 높이(median) 대비 비율로 정의해, DPI·폰트 크기가 달라도 같은 비율로 동작하게 한다.
    typical_line_height = median(
        [max(1.0, bbox[3] - bbox[1]) for bbox, _ in text_lines]
    ) if text_lines else 1.0
    added_groups = 0
    for bbox, confidence in fallback_groups:
        # 새 그룹이 기존 abstract 블록 바로 위/아래에 붙어 있고(간격 ≤ 줄 높이 ×
        # abstract_merge_gap_ratio) 같은 컬럼에 속하면(가로 겹침 ≥ abstract_merge_x_overlap),
        # 별도 블록을 새로 만들지 않고 그 초록 블록에 흡수시킨다. 초록이 여러 조각으로 갈라져
        # 인식되는 문제(docs/guide/12 §5 "초록 앞부분이 별도 본문으로 떨어지던 문제")에 대한
        # 대응이며, 병합 폭을 본문 병합(merge_gap_ratio)보다 좁게 잡아 다른 섹션과 잘못
        # 합쳐지는 것을 방지한다.
        abstract_index = _adjacent_abstract_index(
            updated,
            bbox,
            max_gap=typical_line_height * abstract_merge_gap_ratio,
            min_x_overlap=abstract_merge_x_overlap,
        )
        if abstract_index is not None:
            block = updated[abstract_index]
            if block.bbox is not None:
                updated[abstract_index] = block.model_copy(
                    update={"bbox": _bbox_union(block.bbox, bbox)}
                )
                expanded_indices.add(abstract_index)
                continue
        # 흡수할 초록 블록이 없으면 새 본문(text) 블록으로 추가한다 — 레이아웃 모델이 완전히
        # 놓친 본문 영역을 보충하는 핵심 경로다.
        updated.append(
            LayoutBlock(
                page=page,
                block_type="text",
                text="",
                order=start_order + len(updated),
                bbox=bbox,
                confidence=confidence,
                ocr_engine=None,
            )
        )
        added_groups += 1
    reconciled = _order_detected_blocks(
        deduplicate_layout_blocks(updated),
        start_order=start_order,
    )
    # 위 확장·추가 이후에도 여전히 어떤 블록에도 충분히 덮이지 않는 텍스트 줄이 남을 수
    # 있다(예: 확장 대상에서 제외된 블록 근처, 혹은 그룹핑 임계값 밖에 있던 줄). 이런 잔여
    # 누락분을 한 번 더 그룹핑해 최종적으로 빈 본문 블록으로 복구한다("finally_covered"
    # 지표가 이 2차 복구까지 반영한 최종 커버리지다).
    finally_uncovered = [
        (line_bbox, line_score)
        for line_bbox, line_score in text_lines
        if not any(
            _bbox_intersection_fraction(line_bbox, block.bbox) >= coverage_threshold
            for block in reconciled
        )
    ]
    recovery_groups = _merge_unassigned_text_lines(
        finally_uncovered,
        gap_ratio=merge_gap_ratio,
    )
    for bbox, confidence in recovery_groups:
        reconciled.append(
            LayoutBlock(
                page=page,
                block_type="text",
                text="",
                order=start_order + len(reconciled),
                bbox=bbox,
                confidence=confidence,
                ocr_engine=None,
            )
        )
    if recovery_groups:
        added_groups += len(recovery_groups)
        reconciled = _order_detected_blocks(reconciled, start_order=start_order)
    finally_covered = sum(
        any(
            _bbox_intersection_fraction(line_bbox, block.bbox) >= coverage_threshold
            for block in reconciled
        )
        for line_bbox, _ in text_lines
    )
    return reconciled, {
        "detected_text_lines": len(text_lines),
        "initially_covered_text_lines": initially_covered,
        "finally_covered_text_lines": finally_covered,
        "expanded_blocks": len(expanded_indices),
        "added_text_blocks": added_groups,
    }


def _split_merged_section_regions(
    blocks: list[LayoutBlock],
    text_lines: list[tuple[tuple[float, float, float, float], float | None]],
    *,
    page: int,
    start_order: int,
    enabled: bool,
    min_body_lines: int,
    max_heading_width_ratio: float,
    line_overlap: float,
) -> tuple[list[LayoutBlock], int]:
    """abstract/section_header 블록에 섞여 들어간 "제목 줄 + 본문 여러 줄"을 두 블록으로 분리한다.

    레이아웃 모델이 섹션 제목 한 줄과 그 아래 본문 전체를 하나의 큰 블록으로 뭉쳐 인식하는
    경우가 있다. 이 함수는 블록 내부의 텍스트 검출선을 위→아래로 정렬했을 때 첫 줄이 나머지
    줄들보다 눈에 띄게 짧고(제목다움) 실제로 그 아래에 본문이 이어지면, 첫 줄을 별도의
    section_header 블록으로 분리하고 나머지를 본문(초록이면 abstract 유지, 섹션 제목이면
    text)으로 되돌린다. `analyze_layout` 2단계 보정.
    """
    if not enabled or not text_lines:
        return blocks, 0

    split_blocks: list[LayoutBlock] = []
    split_count = 0
    for block in blocks:
        if block.block_type not in {"abstract", "section_header"} or block.bbox is None:
            split_blocks.append(block)
            continue
        region_lines = sorted(
            [
                (bbox, score)
                for bbox, score in text_lines
                if _bbox_intersection_fraction(bbox, block.bbox) >= line_overlap
            ],
            key=lambda item: (item[0][1], item[0][0]),
        )
        # 첫 줄(제목 후보)을 뺀 나머지가 "본문"으로 인정할 최소 줄 수(min_body_lines) 이상
        # 있어야 분리를 시도한다 — 짧은 한두 줄짜리 블록을 억지로 "제목+본문"으로 쪼개는
        # 오탐을 막기 위함이다.
        if len(region_lines) < min_body_lines + 1:
            split_blocks.append(block)
            continue
        heading_bbox, heading_score = region_lines[0]
        body_lines = region_lines[1:]
        heading_width = max(0.0, heading_bbox[2] - heading_bbox[0])
        median_body_width = median(
            max(1.0, bbox[2] - bbox[0]) for bbox, _ in body_lines
        )
        heading_center_y = (heading_bbox[1] + heading_bbox[3]) / 2
        next_center_y = (body_lines[0][0][1] + body_lines[0][0][3]) / 2
        # 두 가지 조건 중 하나라도 어긋나면 분리하지 않는다: (1) 첫 줄의 폭이 본문 줄 폭
        # 중앙값 대비 max_heading_width_ratio(기본 0.72)를 넘으면 "제목치고 너무 길다"고 보고
        # 포기한다(제목은 보통 본문보다 짧다는 휴리스틱, 실측 근거 문서 없음 — 추정값).
        # (2) 다음 줄의 세로 중심이 첫 줄의 세로 중심보다 위에 있으면(next_center_y <=
        # heading_center_y) 애초에 위→아래 순서 가정이 깨진 것이므로 분리를 포기한다.
        if (
            heading_width / median_body_width > max_heading_width_ratio
            or next_center_y <= heading_center_y
        ):
            split_blocks.append(block)
            continue

        body_bbox = body_lines[0][0]
        for line_bbox, _ in body_lines[1:]:
            body_bbox = _bbox_union(body_bbox, line_bbox)
        if block.block_type == "abstract":
            # 초록 블록을 분리할 때는, 혹시 다른 레이아웃 박스가 이미 이 제목 줄을
            # section_header로 검출해뒀다면(중복) 새 블록을 또 만들지 않는다 — 중복 제거는
            # `deduplicate_layout_blocks`에도 있지만 여기서 미리 걸러 불필요한 블록 생성을 줄인다.
            existing_heading = any(
                other is not block
                and other.block_type == "section_header"
                and _bbox_intersection_fraction(heading_bbox, other.bbox) >= line_overlap
                for other in blocks
            )
            if not existing_heading:
                split_blocks.append(
                    LayoutBlock(
                        page=page,
                        block_type="section_header",
                        text="",
                        order=block.order,
                        bbox=heading_bbox,
                        confidence=heading_score or block.confidence,
                        ocr_engine=None,
                    )
                )
            split_blocks.append(block.model_copy(update={"bbox": body_bbox}))
        else:
            split_blocks.append(block.model_copy(update={"bbox": heading_bbox}))
            split_blocks.append(
                LayoutBlock(
                    page=page,
                    block_type="text",
                    text="",
                    order=block.order + 1,
                    bbox=body_bbox,
                    confidence=block.confidence,
                    ocr_engine=None,
                )
            )
        split_count += 1
    return (
        _order_detected_blocks(split_blocks, start_order=start_order),
        split_count,
    )


def _inline_abstract_candidate_bboxes(
    blocks: list[LayoutBlock],
    text_lines: list[tuple[tuple[float, float, float, float], float | None]],
    *,
    min_body_lines: int,
    line_overlap: float,
    include_text_blocks: bool,
) -> list[tuple[float, float, float, float]]:
    """"제목+본문이 한 줄에 붙어 있을 수 있는" 블록의 첫 줄 bbox만 순수 기하 조건으로 추린다.

    `_split_merged_section_regions`와 달리 여기서는 텍스트를 아직 인식하지 않는다(비용이 큰
    OCR을 후보에게만 적용하기 위한 사전 필터링 단계). `include_text_blocks`는 1페이지에서만
    True로 전달되는데, 본문(text)으로 잘못 분류된 초록도 이 페이지에서는 후보에 포함시키기
    위함이다(제목 다음 페이지에는 이런 오분류가 드물다는 전제).
    """
    candidates: list[tuple[float, float, float, float]] = []
    for block in blocks:
        eligible_types = {"abstract", "text"} if include_text_blocks else {"abstract"}
        if block.block_type not in eligible_types or block.bbox is None:
            continue
        region_lines = sorted(
            [
                bbox
                for bbox, _ in text_lines
                if _bbox_intersection_fraction(bbox, block.bbox) >= line_overlap
            ],
            key=lambda bbox: (bbox[1], bbox[0]),
        )
        if len(region_lines) >= min_body_lines + 1 and region_lines[0] not in candidates:
            candidates.append(region_lines[0])
    return candidates


def _recognize_heading_candidates(
    recognizer: Any,
    image_path: Path,
    candidates: list[tuple[float, float, float, float]],
    *,
    output_dir: Path,
    page: int,
    min_confidence: float,
) -> dict[tuple[float, float, float, float], str]:
    """인라인 초록/제목 분리 후보 줄만 crop해 텍스트 인식을 실행하고, 결과를 bbox별로 모은다.

    신뢰도가 `min_confidence`(기본 0.7) 미만인 인식 결과는 버린다 — 오인식된 텍스트로
    "Abstract" 패턴을 잘못 매칭해 불필요하게 블록을 분리하는 것을 막기 위함이다(실측 근거
    문서 없음 — 추정값).
    """
    try:
        from PIL import Image
    except ImportError as exc:
        raise ImportError("인라인 섹션 제목 분리에는 Pillow가 필요합니다.") from exc

    recognized: dict[tuple[float, float, float, float], str] = {}
    with Image.open(image_path) as image:
        for index, bbox in enumerate(candidates):
            x1, y1, x2, y2 = bbox
            crop_box = (
                max(0, int(x1)),
                max(0, int(y1)),
                min(image.width, int(x2 + 1)),
                min(image.height, int(y2 + 1)),
            )
            if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
                continue
            crop_path = output_dir / f"heading-{page}-{index}.png"
            image.crop(crop_box).save(crop_path)
            results = list(recognizer.predict(str(crop_path)))
            if not results:
                continue
            payload = _as_mapping(results[0])
            text = str(payload.get("rec_text", "")).strip()
            try:
                confidence = float(payload.get("rec_score", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
            if text and confidence >= min_confidence:
                recognized[bbox] = text
    return recognized


def _split_inline_abstract_headings(
    blocks: list[LayoutBlock],
    text_lines: list[tuple[tuple[float, float, float, float], float | None]],
    recognized_headings: dict[tuple[float, float, float, float], str],
    *,
    page: int,
    start_order: int,
    min_body_lines: int,
    line_overlap: float,
    max_prefix_ratio: float,
    include_text_blocks: bool,
) -> tuple[list[LayoutBlock], int]:
    """인식된 첫 줄 텍스트가 실제로 "Abstract"/"초록"/"요약"으로 시작하면 제목/본문을 분리한다.

    `_inline_abstract_candidate_bboxes`가 기하 조건만으로 추린 후보 중, OCR로 확인해
    `ABSTRACT_HEADER_RE`와 일치하는 것만 실제로 분리를 수행하는 3단계 보정의 마지막 단계다.
    "Abstract This paper presents..."처럼 제목과 본문이 같은 줄에 붙어 있으면 문자 위치
    비율(prefix_ratio)로 줄 폭을 좌우로 나눠 제목 부분과 본문 부분의 bbox를 근사한다.
    """
    split_blocks: list[LayoutBlock] = []
    split_count = 0
    for block in blocks:
        eligible_types = {"abstract", "text"} if include_text_blocks else {"abstract"}
        if block.block_type not in eligible_types or block.bbox is None:
            split_blocks.append(block)
            continue
        region_lines = sorted(
            [
                bbox
                for bbox, _ in text_lines
                if _bbox_intersection_fraction(bbox, block.bbox) >= line_overlap
            ],
            key=lambda bbox: (bbox[1], bbox[0]),
        )
        if len(region_lines) < min_body_lines + 1:
            split_blocks.append(block)
            continue
        first_line = region_lines[0]
        recognized_text = recognized_headings.get(first_line, "")
        heading_match = ABSTRACT_HEADER_RE.match(recognized_text)
        if heading_match is None:
            split_blocks.append(block)
            continue
        remainder = recognized_text[heading_match.end() :].strip()
        heading_bbox = first_line
        first_body_bbox: tuple[float, float, float, float] | None = None
        if remainder:
            # "Abstract" 매칭 다음에 이어지는 본문이 있으면(같은 줄에 제목+본문이 붙은 경우),
            # 매칭된 글자 수 비율(prefix_ratio)만큼 줄 bbox를 가로로 잘라 제목/본문 영역을
            # 근사한다. 문자 폭이 균일하다는 가정 하의 근사치이므로, 이 비율이 너무 크면
            # (max_prefix_ratio 초과, 기본 0.4) "제목이 줄 대부분을 차지"하는 비정상적인
            # 경우로 보고 분리를 포기한다(실측 근거 문서 없음 — 추정값).
            prefix_ratio = (heading_match.end() + 1) / len(recognized_text)
            if prefix_ratio > max_prefix_ratio:
                split_blocks.append(block)
                continue
            split_x = first_line[0] + (first_line[2] - first_line[0]) * prefix_ratio
            heading_bbox = (first_line[0], first_line[1], split_x, first_line[3])
            first_body_bbox = (split_x, first_line[1], first_line[2], first_line[3])

        split_blocks.append(
            LayoutBlock(
                page=page,
                block_type="section_header",
                text="",
                order=block.order,
                bbox=heading_bbox,
                confidence=block.confidence,
                ocr_engine=None,
            )
        )
        if first_body_bbox is not None:
            split_blocks.append(
                block.model_copy(
                    update={"block_type": "abstract", "bbox": first_body_bbox}
                )
            )
        remaining_body_bbox = region_lines[1]
        for line_bbox in region_lines[2:]:
            remaining_body_bbox = _bbox_union(remaining_body_bbox, line_bbox)
        split_blocks.append(
            block.model_copy(
                update={"block_type": "abstract", "bbox": remaining_body_bbox}
            )
        )
        split_count += 1
    return (
        _order_detected_blocks(split_blocks, start_order=start_order),
        split_count,
    )


def _recover_page_title_region(
    blocks: list[LayoutBlock],
    text_lines: list[tuple[tuple[float, float, float, float], float | None]],
    *,
    page: int,
    enabled: bool,
    min_width_ratio: float,
) -> tuple[list[LayoutBlock], int]:
    """1페이지에 title 블록이 없을 때, 위치·형태 조건으로 가장 그럴듯한 text 블록을 제목으로 승격한다.

    `analyze_layout` 4단계 보정. 실제 LayoutLMv2 논문 실측에서 첫 페이지 제목이 레이아웃
    모델에서 누락된 사례가 확인됐다(docs/guide/10-production-readiness.md 4단계
    "실제 LayoutLMv2 첫 페이지 제목·Abstract 누락"). "제목"으로 볼 조건은 (1) 초록/섹션제목보다
    위쪽에 있고 (2) 페이지 폭 대비 충분히 넓으며(min_width_ratio, 기본 0.55 — 제목은 보통
    한 줄을 넓게 차지한다는 휴리스틱, 실측 근거 문서 없음 — 추정값) (3) 세로보다 가로가 긴
    형태(가로로 퍼진 한 줄)여야 한다. 후보가 여러 개면 가장 위에 있고 가장 넓은 것을 택한다.
    """
    if not enabled or page != 1 or any(block.block_type == "title" for block in blocks):
        return blocks, 0
    positioned_lines = [bbox for bbox, _ in text_lines]
    if not positioned_lines:
        return blocks, 0
    page_min_x = min(bbox[0] for bbox in positioned_lines)
    page_max_x = max(bbox[2] for bbox in positioned_lines)
    page_width = page_max_x - page_min_x
    if page_width <= 0:
        return blocks, 0
    # 초록/섹션제목이 시작되는 세로 위치보다 위쪽에 있는 블록만 제목 후보로 본다 — 제목은
    # 항상 본문보다 앞에 나온다는 문서 구조 가정이다.
    boundary_y = min(
        (
            block.bbox[1]
            for block in blocks
            if block.bbox is not None
            and block.block_type in {"abstract", "section_header"}
        ),
        default=float("inf"),
    )
    candidates = [
        block
        for block in blocks
        if block.block_type == "text"
        and block.bbox is not None
        and block.bbox[3] < boundary_y
        and block.bbox[2] - block.bbox[0] >= page_width * min_width_ratio
        and block.bbox[2] - block.bbox[0] >= block.bbox[3] - block.bbox[1]
    ]
    if not candidates:
        return blocks, 0
    title = min(candidates, key=lambda block: (block.bbox[1], -(block.bbox[2] - block.bbox[0])))
    return [
        block.model_copy(update={"block_type": "title"}) if block is title else block
        for block in blocks
    ], 1


def _recover_author_block_types(
    blocks: list[LayoutBlock],
    *,
    enabled: bool,
) -> tuple[list[LayoutBlock], int]:
    """제목과 초록/섹션제목 사이에 낀 text 블록을 author로 재분류한다.

    `analyze_layout` 5단계 보정. 실제 판단 로직은 `_probable_author_region_orders`에
    있으며, 저자 줄이 레이아웃 모델에서 `author`가 아닌 일반 `text`로 분류되는 실제 사례
    (docs/guide/12-macbook-remote-development-handoff.md §6)로 인한 저자 메타데이터
    결측을 줄이기 위한 보정이다.
    """
    if not enabled:
        return blocks, 0
    author_orders = _probable_author_region_orders(blocks)
    recovered = 0
    normalized: list[LayoutBlock] = []
    for block in blocks:
        if block.order in author_orders and block.block_type == "text":
            normalized.append(block.model_copy(update={"block_type": "author"}))
            recovered += 1
        else:
            normalized.append(block)
    return normalized, recovered


def _adjacent_abstract_index(
    blocks: list[LayoutBlock],
    bbox: tuple[float, float, float, float],
    *,
    max_gap: float,
    min_x_overlap: float,
) -> int | None:
    """주어진 bbox와 세로로 가깝고 같은 컬럼에 있는 abstract 블록의 인덱스를 찾는다.

    여러 abstract 후보가 조건을 만족하면 세로 간격이 가장 작은(가장 가까운) 것을 택한다.
    `_reconcile_layout_with_text`에서 누락 텍스트를 초록에 흡수시킬지 판단하는 데 쓰인다.
    """
    candidates: list[tuple[float, int]] = []
    for index, block in enumerate(blocks):
        if block.block_type != "abstract" or block.bbox is None:
            continue
        vertical_gap = max(
            0.0,
            block.bbox[1] - bbox[3],
            bbox[1] - block.bbox[3],
        )
        if vertical_gap > max_gap:
            continue
        if _horizontal_overlap_fraction(bbox, block.bbox) < min_x_overlap:
            continue
        candidates.append((vertical_gap, index))
    return min(candidates)[1] if candidates else None


def _merge_unassigned_text_lines(
    lines: list[tuple[tuple[float, float, float, float], float | None]],
    *,
    gap_ratio: float,
) -> list[tuple[tuple[float, float, float, float], float | None]]:
    """레이아웃에 배정되지 않은 텍스트 줄들을 위→아래 순서로 훑으며 인접한 줄끼리 한 그룹으로 묶는다.

    "인접"의 기준은 절대 픽셀이 아니라 이 그룹의 전형적인 줄 높이(median) × gap_ratio로
    정의해 DPI·폰트 크기 변화에 견고하게 한다. 세로 간격 조건과 함께 가로 겹침이 30%
    이상이어야(같은 컬럼) 병합하므로, 2단 조판에서 서로 다른 컬럼의 줄이 잘못 섞이지 않는다.
    그룹의 confidence는 포함된 줄들의 평균으로 근사한다.
    """
    if not lines:
        return []
    heights = [max(1.0, bbox[3] - bbox[1]) for bbox, _ in lines]
    max_gap = median(heights) * gap_ratio
    groups: list[tuple[tuple[float, float, float, float], list[float]]] = []
    for bbox, score in sorted(lines, key=lambda item: (item[0][1], item[0][0])):
        match_index = next(
            (
                index
                for index, (group_bbox, _) in enumerate(groups)
                if 0.0 <= bbox[1] - group_bbox[3] <= max_gap
                and _horizontal_overlap_fraction(bbox, group_bbox) >= 0.3
            ),
            None,
        )
        if match_index is None:
            groups.append((bbox, [score] if score is not None else []))
            continue
        group_bbox, scores = groups[match_index]
        groups[match_index] = (
            _bbox_union(group_bbox, bbox),
            [*scores, *([score] if score is not None else [])],
        )
    return [
        (bbox, sum(scores) / len(scores) if scores else None)
        for bbox, scores in groups
    ]


def _bbox_intersection_fraction(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float] | None,
) -> float:
    """교집합 넓이 / inner 넓이. 비대칭 지표이므로 "inner가 outer에 얼마나 포함되는지"를 뜻한다."""
    if outer is None:
        return 0.0
    intersection = max(0.0, min(inner[2], outer[2]) - max(inner[0], outer[0])) * max(
        0.0, min(inner[3], outer[3]) - max(inner[1], outer[1])
    )
    area = max(0.0, inner[2] - inner[0]) * max(0.0, inner[3] - inner[1])
    return intersection / area if area > 0 else 0.0


def _bbox_vertical_overlap_fraction(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float] | None,
) -> float:
    """세로 겹침 길이 / first의 세로 길이. "first가 second와 세로로 얼마나 정렬돼 있는지"를 뜻한다."""
    if second is None:
        return 0.0
    overlap = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    first_height = max(0.0, first[3] - first[1])
    return overlap / first_height if first_height > 0 else 0.0


def _horizontal_overlap_fraction(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    """가로 겹침 길이 / 두 bbox 중 더 좁은 쪽의 가로 길이. 두 bbox가 같은 컬럼에 속하는지 판단하는 데 쓰인다."""
    overlap = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    smaller_width = min(first[2] - first[0], second[2] - second[0])
    return overlap / smaller_width if smaller_width > 0 else 0.0


def _bbox_union(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """두 bbox를 모두 포함하는 최소 사각형(합집합)을 반환한다."""
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )


def _order_detected_blocks(
    blocks: list[LayoutBlock],
    *,
    start_order: int,
) -> list[LayoutBlock]:
    """블록들을 논문의 실제 읽기 순서(2단 조판 좌→우 열 순서 포함)로 재정렬하고 `order`를 매긴다.

    단순히 세로 좌표로만 정렬하면 2단 조판에서 왼쪽 열 중간에 오른쪽 열 상단 블록이 끼어드는
    잘못된 순서가 나온다. 이 함수는 페이지 폭 중앙(midpoint)을 기준으로 좌/우 컬럼을 나누고,
    "컬럼을 가로지르는"(제목·초록처럼 폭 전체를 차지하는) 블록을 컬럼 경계선(band)으로 삼아
    그 사이 구간별로 좌→우, 위→아래 순서를 적용한다. 좌/우 각각 블록이 2개 미만이면 2단
    조판이 아니라고 보고 단순 위→아래·왼쪽→오른쪽 정렬로 대체한다(오탐 방지).
    """
    positioned = [block for block in blocks if block.bbox is not None]
    if len(positioned) < 2:
        return blocks
    left_edge = min(block.bbox[0] for block in positioned if block.bbox is not None)
    right_edge = max(block.bbox[2] for block in positioned if block.bbox is not None)
    content_width = right_edge - left_edge
    midpoint = (left_edge + right_edge) / 2.0
    # 중앙선에서 좌우로 컨텐츠 폭의 8%(실측 근거 문서 없음 — 추정값) 이상 벗어나 양쪽 모두
    # 걸쳐 있어야 "컬럼을 가로지르는" 블록으로 인정한다. 여백만 살짝 걸치는 좁은 블록이
    # 실수로 컬럼 경계(band)로 오인되는 것을 막기 위한 여유값이다.
    spanning_margin = content_width * 0.08

    def spans_columns(block: LayoutBlock) -> bool:
        if block.bbox is None:
            return False
        x1, _, x2, _ = block.bbox
        return x1 < midpoint - spanning_margin and x2 > midpoint + spanning_margin

    left = [
        block
        for block in positioned
        if not spans_columns(block) and block.bbox[0] < midpoint
    ]
    right = [
        block
        for block in positioned
        if not spans_columns(block) and block.bbox[0] >= midpoint
    ]
    if len(left) < 2 or len(right) < 2:
        # 좌/우 어느 한쪽에 블록이 거의 없으면 2단 조판이 아니라(예: 1단 논문, 표지 페이지)
        # 판단하고 좌표 기반 단순 정렬로 대체한다.
        ordered = sorted(positioned, key=_top_left_key)
    else:
        # 컬럼을 가로지르는 블록들(제목·초록·표 등)을 세로 위치 순으로 정렬해 "구간
        # 경계(band)" 목록으로 삼는다. 예를 들어 [제목, 초록]이 있으면 페이지는 3구간으로
        # 나뉜다: 제목 위, 제목~초록 사이, 초록 아래.
        spanning = sorted(
            (block for block in positioned if spans_columns(block)),
            key=_top_left_key,
        )
        bands: list[list[LayoutBlock]] = [[] for _ in range(len(spanning) + 1)]
        for block in positioned:
            if spans_columns(block):
                continue
            center_y = (block.bbox[1] + block.bbox[3]) / 2.0
            # 이 블록보다 위에 있는(bbox 하단이 블록 중심보다 작은) spanning 블록의 개수가
            # 곧 이 블록이 속한 구간(band) 인덱스다.
            band_index = sum(
                span.bbox is not None and span.bbox[3] <= center_y for span in spanning
            )
            bands[band_index].append(block)
        ordered = []
        for index, band in enumerate(bands):
            # 같은 구간 안에서는 왼쪽 컬럼을 통째로 먼저 읽고 오른쪽 컬럼을 그다음에 읽는다
            # (논문의 실제 읽기 순서: 왼쪽 열 전체 → 오른쪽 열 전체).
            ordered.extend(
                sorted(
                    band,
                    key=lambda block: (
                        0 if block.bbox[0] < midpoint else 1,
                        block.bbox[1],
                        block.bbox[0],
                    ),
                )
            )
            if index < len(spanning):
                ordered.append(spanning[index])
    # bbox가 없는 블록(예: 텍스트 좌표 없이 생성된 fallback)은 순서를 정할 수 없으므로 맨
    # 뒤에 그대로 붙인다.
    unpositioned = [block for block in blocks if block.bbox is None]
    ordered.extend(unpositioned)
    return [
        block.model_copy(update={"order": start_order + index})
        for index, block in enumerate(ordered)
    ]


def _normalize_semantic_block_types(
    blocks: list[LayoutBlock],
    *,
    recover_author_region: bool = True,
) -> list[LayoutBlock]:
    """`recognize_layout`(사람이 확정한 블록) 결과에 대해 명백한 유형 오류만 최소로 교정한다.

    사람이 검수를 마친 뒤에도 남을 수 있는 두 가지 패턴을 고친다: (1) 저자 위치 휴리스틱상
    author로 볼 여지가 있는데 여전히 text로 남은 블록, (2) 본문 섹션이 이미 시작된
    뒤(section_header를 만난 뒤)에도 abstract로 남아 있는 블록 — 오른쪽 열의 본문이
    "abstract"로 오분류되던 실제 사례(docs/guide/12-macbook-remote-development-handoff.md
    §5 "오른쪽 열의 applications...가 abstract로 오분류되던 문제")에 대한 사후 교정이다.
    ABSTRACT_HEADER_RE에 매칭되는 section_header(즉 "Abstract"로 시작하는 제목 그 자체)는
    본문 시작으로 치지 않는다.
    """
    ordered = sorted(blocks, key=lambda item: item.order)
    author_orders = (
        _probable_author_region_orders(ordered) if recover_author_region else set()
    )
    body_started_by_page: dict[int, bool] = {}
    normalized: list[LayoutBlock] = []
    for block in ordered:
        if block.order in author_orders and block.block_type == "text":
            normalized.append(block.model_copy(update={"block_type": "author"}))
            continue
        body_started = body_started_by_page.get(block.page, False)
        if block.block_type == "section_header":
            if ABSTRACT_HEADER_RE.match(block.text.strip()):
                normalized.append(block)
                continue
            body_started_by_page[block.page] = True
            normalized.append(block)
            continue
        if block.block_type == "abstract" and body_started:
            normalized.append(block.model_copy(update={"block_type": "text"}))
            continue
        normalized.append(block)
    return normalized


def _probable_author_region_orders(blocks: list[LayoutBlock]) -> set[int]:
    """첫 페이지에서 "제목 다음, 초록/섹션제목 이전"에 있는 가로로 퍼진 text 블록들의 order 집합을 찾는다.

    논문의 통상적인 구조(제목 → 저자 → 초록)를 가정한 순수 위치 기반 휴리스틱이다. 제목이나
    초록/섹션제목 경계를 찾지 못하면 안전하게 빈 집합을 반환한다(저자 복구를 시도하지 않음).
    """
    if not blocks:
        return set()
    first_page = min(block.page for block in blocks)
    page_blocks = [block for block in blocks if block.page == first_page]
    title_orders = [block.order for block in page_blocks if block.block_type == "title"]
    if not title_orders:
        return set()
    title_end = max(title_orders)
    boundary_orders = [
        block.order
        for block in page_blocks
        if block.order > title_end
        and block.block_type in {"abstract", "section_header"}
    ]
    if not boundary_orders:
        return set()
    boundary = min(boundary_orders)
    return {
        block.order
        for block in page_blocks
        if title_end < block.order < boundary
        and block.block_type == "text"
        and (
            block.bbox is None
            or block.bbox[2] - block.bbox[0] >= block.bbox[3] - block.bbox[1]
        )
    }


def _top_left_key(block: LayoutBlock) -> tuple[float, float]:
    """(위쪽 y, 왼쪽 x) 순으로 정렬하기 위한 키. bbox가 없으면 항상 맨 뒤로 보낸다."""
    if block.bbox is None:
        return (float("inf"), float("inf"))
    return (block.bbox[1], block.bbox[0])


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """PaddleX/PaddleOCR 예측 결과 객체에서 실제 데이터 Mapping을 안전하게 꺼낸다.

    PaddleX 버전에 따라 결과 객체가 `.json()` 메서드를 갖거나(그 안에 `res` 키로 한 번 더
    감싸기도 함), 자체가 이미 Mapping이거나, `.res` 속성을 직접 갖는 등 형태가 제각각이라
    이 함수가 그 차이를 흡수하는 단일 진입점 역할을 한다. 어떤 형태에도 맞지 않으면 빈
    dict를 반환해 호출자가 `.get()`으로 안전하게 이어갈 수 있게 한다.
    """
    json_value = getattr(value, "json", None)
    if callable(json_value):
        json_value = json_value()
    if isinstance(json_value, Mapping):
        nested = json_value.get("res")
        return nested if isinstance(nested, Mapping) else json_value
    if isinstance(value, Mapping):
        return value
    data = getattr(value, "res", None)
    return data if isinstance(data, Mapping) else {}


def _best_table_classification(
    payload: Mapping[str, Any],
) -> tuple[str | None, float | None]:
    """PP-LCNet 표 분류 결과에서 최고 점수 라벨을 "wired"/"wireless"로 정규화해 반환한다."""
    labels = payload.get("label_names")
    scores = payload.get("scores")
    if not isinstance(labels, list) or not isinstance(scores, list) or not scores:
        return None, None
    best_index = max(range(len(scores)), key=lambda index: float(scores[index]))
    if best_index >= len(labels):
        return None, None
    label = str(labels[best_index]).strip().lower()
    normalized = {
        "wired_table": "wired",
        "wireless_table": "wireless",
    }.get(label)
    return normalized, float(scores[best_index])


def _find_block_candidates(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """PP-StructureV3 결과 payload에서 실제 레이아웃 블록 목록이 들어 있는 키를 재귀적으로 찾는다.

    PaddleX 버전·파이프라인 구성에 따라 블록 목록이 `parsing_res_list`, `layout`, `blocks`,
    `layout_det_res` 중 어느 키 아래(혹은 그 안에 한 번 더 중첩되어)에 있을지 달라질 수 있어,
    후보 키를 순서대로 시도하고 실제로 Mapping 원소를 담은 리스트를 찾으면 즉시 반환한다.
    """
    for key in ("parsing_res_list", "layout", "blocks", "layout_det_res"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            nested = _find_block_candidates(value)
            if nested:
                return nested
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
            rows = [row for row in value if isinstance(row, Mapping)]
            if rows:
                return rows
    return []


def _first_text(row: Mapping[str, Any], *keys: str) -> str:
    """주어진 키들을 순서대로 확인해 처음으로 비어있지 않은 문자열 값을 반환한다."""
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _normalize_label(label: str) -> str:
    """PP-StructureV3/PP-DocLayout 라벨 표기를 `LABEL_MAP`의 키 형식(소문자 스네이크케이스)으로 맞춘다."""
    return label.strip().lower().replace("-", "_").replace(" ", "_")


def _bbox(row: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    """다양한 좌표 표현(dict `{x1,y1,x2,y2}` 또는 리스트 `[x1,y1,x2,y2,...]`)을 (x1,y1,x2,y2)로 정규화한다."""
    value = row.get("block_bbox", row.get("bbox", row.get("coordinate")))
    if isinstance(value, Mapping):
        values = [value.get(key) for key in ("x1", "y1", "x2", "y2")]
    elif isinstance(value, (list, tuple)) and len(value) >= 4:
        values = list(value[:4])
    else:
        return None
    try:
        x1, y1, x2, y2 = (float(item) for item in values)
    except (TypeError, ValueError):
        return None
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def _score(row: Mapping[str, Any]) -> float | None:
    """신뢰도 점수를 여러 가능한 키(`score`/`confidence`/`rec_score`)에서 찾아 [0, 1] 범위로 자른다."""
    value = row.get("score", row.get("confidence", row.get("rec_score")))
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def _page_number(row: Mapping[str, Any], fallback: int) -> int:
    """결과 행에 페이지 번호가 있으면 사용하고, 없거나 파싱 실패하면 호출자가 아는 페이지 번호로 대체한다."""
    value = row.get("page", row.get("page_id", row.get("page_no", fallback)))
    try:
        page = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(1, page)


def _ocr_text_in_bbox(
    payload: Mapping[str, Any],
    bbox: tuple[float, float, float, float],
) -> str:
    """표 bbox 안에 중심점이 들어오는 전체 OCR 토큰들을 행·열로 정렬해 "|"-구분 텍스트로 만든다.

    `_map_result`(PP-StructureV3 단일 경로)에서 표의 markdown/content가 비어 있을 때 쓰는
    대체 경로다. 정밀 표 구조화(SLANeXt/SLANet_plus)를 거치지 않으므로 셀 병합 등은 반영하지
    못하지만, 토큰의 세로 중심 좌표를 기준으로 "같은 행"을 판정해(row_tolerance) 최소한
    표 형태를 흉내 낸 텍스트를 보존한다.
    """
    ocr = payload.get("overall_ocr_res")
    if not isinstance(ocr, Mapping):
        return ""
    texts = ocr.get("rec_texts")
    boxes = ocr.get("rec_boxes")
    if not isinstance(texts, list) or not isinstance(boxes, list):
        return ""

    x1, y1, x2, y2 = bbox
    cells: list[tuple[float, float, float, str]] = []
    for text, box in zip(texts, boxes, strict=False):
        if not isinstance(box, (list, tuple)) or len(box) < 4:
            continue
        try:
            bx1, by1, bx2, by2 = (float(value) for value in box[:4])
        except (TypeError, ValueError):
            continue
        center_x = (bx1 + bx2) / 2
        center_y = (by1 + by2) / 2
        normalized = str(text).strip()
        # 토큰의 중심점이 표 bbox 안에 있는 것만 이 표에 속한다고 본다(토큰 bbox 전체가
        # 표 경계와 겹치는지가 아니라 중심점 포함 여부로 판정 — 경계에 살짝 걸친 이웃 토큰의
        # 오분류를 줄이기 위함).
        if normalized and x1 <= center_x <= x2 and y1 <= center_y <= y2:
            cells.append((center_y, center_x, max(1.0, by2 - by1), normalized))
    if not cells:
        return ""

    # 같은 "행"으로 볼 세로 중심 좌표 오차 허용치. 토큰 높이 중앙값의 60%(실측 근거 문서
    # 없음 — 추정값)와 최소 4px 중 큰 값을 쓴다. 행이 확정될 때마다 그 행의 대표 y좌표를
    # 이동평균으로 갱신해(row_centers[-1] 갱신) 살짝 기울어진 스캔에도 어느 정도 견고하다.
    row_tolerance = max(4.0, median(cell[2] for cell in cells) * 0.6)
    rows: list[list[tuple[float, str]]] = []
    row_centers: list[float] = []
    for center_y, center_x, _, text in sorted(cells):
        if not rows or abs(center_y - row_centers[-1]) > row_tolerance:
            rows.append([(center_x, text)])
            row_centers.append(center_y)
        else:
            rows[-1].append((center_x, text))
            row_centers[-1] = (row_centers[-1] + center_y) / 2
    return "\n".join(" | ".join(text for _, text in sorted(row)) for row in rows)


def _unassigned_ocr_blocks(
    payload: Mapping[str, Any],
    existing_blocks: list[LayoutBlock],
    *,
    page: int,
    start_order: int,
) -> list[LayoutBlock]:
    """PP-StructureV3의 전체 OCR 결과 중 기존 레이아웃 블록 어디와도 겹치지 않는 토큰을 새 본문 블록으로 만든다.

    `_map_result`(단일 파이프라인 경로) 전용 안전망이다. 레이아웃 검출이 놓친 텍스트를
    잃어버리지 않기 위한 것이며, 반환된 블록들은 `_merge_in_reading_order`로 기존 블록들과
    함께 읽기 순서에 맞게 재배치된다.
    """
    ocr = payload.get("overall_ocr_res")
    if not isinstance(ocr, Mapping):
        return []
    texts = ocr.get("rec_texts")
    boxes = ocr.get("rec_boxes")
    scores = ocr.get("rec_scores")
    if not isinstance(texts, list) or not isinstance(boxes, list):
        return []

    occupied = [block.bbox for block in existing_blocks if block.bbox is not None]
    fallback: list[LayoutBlock] = []
    for index, (text_value, box_value) in enumerate(zip(texts, boxes, strict=False)):
        text_value = str(text_value).strip()
        bbox = _bbox({"bbox": box_value})
        if not text_value or bbox is None:
            continue
        if any(_has_text_overlap(bbox, region) for region in occupied):
            continue
        confidence = None
        if isinstance(scores, list) and index < len(scores):
            confidence = _score({"score": scores[index]})
        fallback.append(
            LayoutBlock(
                page=page,
                block_type="text",
                text=text_value,
                order=start_order + len(fallback),
                bbox=bbox,
                confidence=confidence,
                ocr_engine="pp-ocrv5-unassigned",
            )
        )
    return fallback


def _has_text_overlap(
    text_bbox: tuple[float, float, float, float],
    layout_bbox: tuple[float, float, float, float],
) -> bool:
    """두 bbox가 3px(실측 근거 문서 없음 — 추정값) 넘게 실질적으로 겹치는지 판단한다.

    `_bbox_intersection_fraction` 같은 비율이 아니라 절대 픽셀 임계값을 쓰는 이유는, 매우
    작은 텍스트 토큰이 큰 레이아웃 블록 경계에 살짝 스친 것만으로 "겹침 없음"으로 오판되지
    않게 하기 위함이다(비율 기준이면 작은 쪽 넓이 대비 아주 미세한 겹침도 비율이 커 보일 수
    있다).
    """
    overlap_width = min(text_bbox[2], layout_bbox[2]) - max(text_bbox[0], layout_bbox[0])
    overlap_height = min(text_bbox[3], layout_bbox[3]) - max(text_bbox[1], layout_bbox[1])
    return overlap_width > 3.0 and overlap_height > 3.0


def _merge_in_reading_order(
    blocks: list[LayoutBlock],
    fallback_blocks: list[LayoutBlock],
    *,
    start_order: int,
) -> list[LayoutBlock]:
    """`_unassigned_ocr_blocks`가 만든 fallback 블록들을 기존 블록 사이 적절한 위치에 끼워 넣는다.

    좌/우 컬럼 각각 2개 미만이면(2단 조판이 아니라고 판단) 전체를 좌표로 단순 정렬한다.
    2단 조판이면 각 fallback 블록이 속한 컬럼(zone) 안에서, 그 블록보다 아래에 있는 첫
    기존 블록 바로 앞에 삽입한다(`anchor`). 같은 zone에 기존 블록이 전혀 없으면 그 zone의
    마지막 순서 뒤에 붙인다. `anchor + fallback_index / 10_000.0`처럼 아주 작은 소수를 더해
    같은 anchor를 공유하는 여러 fallback 블록끼리도 원래 순서를 유지한다.
    """
    all_blocks = [*blocks, *fallback_blocks]
    bboxes = [block.bbox for block in all_blocks if block.bbox is not None]
    if not bboxes:
        return all_blocks
    page_width = max(bbox[2] for bbox in bboxes)
    midpoint = page_width / 2.0

    # 중앙선에서 5%(실측 근거 문서 없음 — 추정값) 여유를 두고 완전히 왼쪽/오른쪽에 있는
    # 블록만 "left"/"right"로 분류하고, 그 여유를 넘어 양쪽에 걸치면 "full"(컬럼 폭 전체)로
    # 취급한다.
    def zone(block: LayoutBlock) -> str:
        if block.bbox is None:
            return "full"
        x1, _, x2, _ = block.bbox
        if x2 <= midpoint * 1.05:
            return "left"
        if x1 >= midpoint * 0.95:
            return "right"
        return "full"

    left_count = sum(zone(block) == "left" for block in blocks)
    right_count = sum(zone(block) == "right" for block in blocks)
    if left_count < 2 or right_count < 2:
        ordered = sorted(
            all_blocks,
            key=lambda block: (
                block.bbox[1] if block.bbox is not None else float("inf"),
                block.bbox[0] if block.bbox is not None else float("inf"),
            ),
        )
    else:
        positions: list[tuple[float, LayoutBlock]] = [
            (float(block.order), block) for block in blocks
        ]
        for fallback_index, fallback in enumerate(fallback_blocks, start=1):
            same_zone = [block for block in blocks if zone(block) == zone(fallback)]
            fallback_y = fallback.bbox[1] if fallback.bbox is not None else float("inf")
            following = [
                block
                for block in same_zone
                if block.bbox is not None and block.bbox[1] > fallback_y
            ]
            if following:
                anchor = min(following, key=lambda block: block.bbox[1]).order - 0.5
            elif same_zone:
                anchor = max(block.order for block in same_zone) + 0.5
            else:
                anchor = max((block.order for block in blocks), default=start_order) + 0.5
            positions.append((anchor + fallback_index / 10_000.0, fallback))
        ordered = [block for _, block in sorted(positions, key=lambda item: item[0])]

    return [
        block.model_copy(update={"order": start_order + index})
        for index, block in enumerate(ordered)
    ]


def _layout_score(
    payload: Mapping[str, Any],
    label: str,
    bbox: tuple[float, float, float, float] | None,
) -> float | None:
    """`_map_result`에서 블록 자체 점수가 없을 때, 같은 라벨의 원시 레이아웃 검출 결과에서 점수를 대체 추정한다.

    같은 라벨을 가진 레이아웃 검출 박스 중 IoU가 가장 큰 것을 찾고, 그 겹침이 0.5(실측
    근거 문서 없음 — 추정값) 이상일 때만 신뢰할 수 있는 매칭으로 보고 점수를 채택한다.
    """
    if bbox is None:
        return None
    layout = payload.get("layout_det_res")
    if not isinstance(layout, Mapping):
        return None
    rows = layout.get("boxes")
    if not isinstance(rows, list):
        return None
    normalized_label = _normalize_label(label)
    matches: list[tuple[float, float]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if _normalize_label(_first_text(row, "label")) != normalized_label:
            continue
        candidate_bbox = _bbox(row)
        candidate_score = _score(row)
        if candidate_bbox is None or candidate_score is None:
            continue
        matches.append((_bbox_iou(bbox, candidate_bbox), candidate_score))
    if not matches:
        return None
    overlap, score = max(matches)
    return score if overlap >= 0.5 else None


def _bbox_iou(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    """표준 IoU(교집합/합집합). `_bbox_intersection_fraction`과 달리 대칭 지표라 "같은 영역인지" 판단에 쓰인다."""
    x1 = max(first[0], second[0])
    y1 = max(first[1], second[1])
    x2 = min(first[2], second[2])
    y2 = min(first[3], second[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    union = first_area + second_area - intersection
    return intersection / union if union > 0 else 0.0


def _render_pdf_pages(
    pdf_path: Path,
    output_dir: Path,
    *,
    dpi: int,
) -> list[tuple[int, Path, float]]:
    """PDF의 모든 페이지를 PNG 이미지로 렌더링한다. 텍스트 레이어는 쓰지 않는다(DESIGN.md STEP 1).

    반환하는 `scale`은 "PDF 포인트(1/72인치) → 렌더링 픽셀" 배율이며, 이후 레이아웃/OCR
    좌표를 원본 PDF 좌표계로 되돌리는 `_scale_blocks`, crop 시 픽셀 좌표로 변환하는
    `_scaled_crop_box`에서 사용한다.
    """
    if dpi <= 0:
        raise ValueError("PAPERRAG_OCR_RENDER_DPI must be positive.")
    try:
        import pypdfium2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError("전체 PDF OCR에는 pypdfium2 페이지 렌더링이 필요합니다.") from exc

    scale = dpi / 72.0
    pages: list[tuple[int, Path, float]] = []
    document = pypdfium2.PdfDocument(pdf_path)
    try:
        for page_index, page in enumerate(document, start=1):
            image_path = output_dir / f"page-{page_index:05d}.png"
            # scale은 pypdfium2 기준 "PDF 포인트 1개당 픽셀 수"로, PyMuPDF의
            # Matrix(scale, scale)과 동일한 의미다(둘 다 dpi/72).
            bitmap = page.render(scale=scale, draw_annots=False)
            bitmap.to_pil().convert("RGB").save(image_path)
            pages.append((page_index, image_path, scale))
    finally:
        document.close()
    return pages


def _scale_blocks(blocks: list[LayoutBlock], scale: float) -> list[LayoutBlock]:
    """렌더링 DPI 배율만큼 커진 bbox 좌표를 원본 PDF 좌표계(포인트 단위)로 되돌린다."""
    if scale == 1.0:
        return blocks
    scaled: list[LayoutBlock] = []
    for block in blocks:
        bbox = block.bbox
        if bbox is not None:
            x1, y1, x2, y2 = bbox
            bbox = (x1 / scale, y1 / scale, x2 / scale, y2 / scale)
        scaled.append(block.model_copy(update={"bbox": bbox}))
    return scaled


def _require_model_directory(path: Path, setting_name: str) -> None:
    """모델 디렉터리가 실제로 존재하고 비어 있지 않은지 미리 검증한다.

    경로 설정이 있음에도 디렉터리가 비어 있으면(예: 다운로드 스크립트를 아직 실행하지
    않음) PaddleX가 온라인에서 몰래 재다운로드를 시도하거나 알기 어려운 내부 오류를
    내는 대신, 폐쇄망/오프라인 운영 환경에서도 바로 알아볼 수 있는 명확한 오류를 낸다.
    """
    if not path.is_dir() or not any(item.is_file() for item in path.rglob("*")):
        raise FileNotFoundError(f"{setting_name}={path} 디렉터리가 없거나 비어 있습니다.")


def _configure_paddle_runtime(settings: Settings) -> None:
    """Paddle/PaddleX 런타임 환경변수를 프로세스 전역으로 1회 설정한다.

    `PADDLE_PDX_CACHE_HOME`은 모델 캐시 위치를 로컬 경로로 고정한다(오프라인 운영 대응).
    MKLDNN(Intel CPU 가속 라이브러리)은 기본적으로 비활성화하는데, sudo 권한이 없는 개발
    머신에서 vendored `libgomp1`을 우회로 쓰는 실행 환경(`scripts/with_paddle_runtime.sh`,
    docs/guide/10-production-readiness.md 1단계)과의 호환성 문제 때문이다
    (`Settings.paddle_enable_mkldnn` 기본값 False). `os.environ.setdefault`를 쓰는 이유는
    이미 다른 경로에서 설정된 값(예: 컨테이너 환경변수)을 덮어쓰지 않기 위함이다.
    """
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(settings.paddlex_cache_dir))
    if not settings.paddle_enable_mkldnn:
        os.environ.setdefault("FLAGS_use_mkldnn", "0")
        os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "False")


def _scaled_crop_box(
    bbox: tuple[float, float, float, float],
    scale: float,
    image_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    """PDF 좌표계의 bbox를 렌더링된 페이지 이미지의 정수 픽셀 crop 좌표로 변환한다.

    `recognize_layout`에서 사람이 확정한 블록(PDF 좌표 저장)을 실제 이미지에서 crop하기
    위해 쓰인다. 우측/하단 경계는 반올림 손실로 텍스트가 잘리지 않도록 0.999를 더해 올림에
    가깝게 처리하고, 모든 좌표를 이미지 크기 안으로 clamp한다.
    """
    width, height = image_size
    x1, y1, x2, y2 = bbox
    return (
        max(0, min(width - 1, int(x1 * scale))),
        max(0, min(height - 1, int(y1 * scale))),
        max(1, min(width, int(x2 * scale + 0.999))),
        max(1, min(height, int(y2 * scale + 0.999))),
    )


def _parse_span(value: str | None) -> int:
    """colspan/rowspan 속성값을 안전하게 정수로 읽는다. 없거나 잘못된 값이면 1(병합 없음)."""
    try:
        parsed = int(value) if value else 1
    except (TypeError, ValueError):
        return 1
    return parsed if parsed >= 1 else 1


class _TableHtmlParser(HTMLParser):
    """SLANeXt/SLANet_plus가 내놓는 표 구조 HTML(`pred_html`)을 행×열 그리드로 파싱한다.

    2026-07-22 수정: 이전에는 colspan/rowspan을 읽지 않고 `<td>` 태그 등장 순서를 그대로
    한 칸씩 채웠다 — 병합 셀이 있으면 그 뒤 모든 열이 한 칸씩 밀려 실제 값이 엉뚱한
    열에 쓰이고(예: 헤더가 2열을 차지하는 표에서 값 전체가 한 칸씩 어긋남), 실측
    적재 논문(LiLT, paper_id=3)의 표 1건에서 실제로 재현됐다(헤더·데이터 여러 행이
    한 셀에 뒤섞여 저장됨). colspan만큼 같은 텍스트를 여러 열에 채우고, rowspan은
    `_pending`에 등록해 다음 `<tr>` 시작 시 해당 열을 먼저 채워 넣는 방식으로
    셀이 차지하는 실제 그리드 위치를 복원한다(TEDS 정밀 평가용은 아니고, "표 안
    글자가 엉뚱한 열로 밀리지 않게" 하기 위한 실용적 그리드 복원이다).
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str | None] | None = None
        self._cell: list[str] | None = None
        self._cell_colspan = 1
        self._cell_rowspan = 1
        # rowspan으로 아래 행까지 이어지는 값: 열 인덱스 -> [남은 행 수, 텍스트]
        self._pending: dict[int, list[Any]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
            self._apply_pending_rowspans()
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
            attr_map = dict(attrs)
            self._cell_colspan = _parse_span(attr_map.get("colspan"))
            self._cell_rowspan = _parse_span(attr_map.get("rowspan"))

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            normalized = " ".join(data.split())
            if normalized:
                self._cell.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            text = " ".join(self._cell).strip()
            self._cell = None
            col = self._next_free_column()
            for offset in range(self._cell_colspan):
                self._set_cell(col + offset, text)
            if self._cell_rowspan > 1:
                for offset in range(self._cell_colspan):
                    self._pending[col + offset] = [self._cell_rowspan - 1, text]
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append([cell if cell is not None else "" for cell in self._row])
            self._row = None

    def _next_free_column(self) -> int:
        assert self._row is not None
        col = 0
        while col < len(self._row) and self._row[col] is not None:
            col += 1
        return col

    def _set_cell(self, col: int, text: str) -> None:
        assert self._row is not None
        while len(self._row) <= col:
            self._row.append(None)
        self._row[col] = text

    def _apply_pending_rowspans(self) -> None:
        for col, (_, text) in list(self._pending.items()):
            self._set_cell(col, text)
        for col in list(self._pending.keys()):
            self._pending[col][0] -= 1
            if self._pending[col][0] <= 0:
                del self._pending[col]


def _html_table_to_pipe_text(html: str) -> str:
    """표 구조 HTML을 Excel 등 평면 텍스트 출력에 적합한 "|"-구분 텍스트로 변환한다.

    셀 값 안에 리터럴 "|"가 있으면(OCR 오인식 등) 재파싱(`excel.py::_parse_table_rows`) 시
    열 구분자와 혼동되므로 "/"로 치환한다.
    """
    if not html.strip():
        return ""
    parser = _TableHtmlParser()
    parser.feed(html)
    return "\n".join(" | ".join(cell.replace("|", "/") for cell in row) for row in parser.rows)


def _table_structure_quality(table_text: str) -> float:
    """"|"-구분 표 텍스트의 구조 일관성을 [0, 1] 범위로 근사 평가하는 임시 지표.

    DESIGN.md §6이 요구하는 정답 기반 TEDS(Tree-Edit-Distance-based Similarity) 평가셋이
    아직 없으므로(docs/reports/assessments/2026-07-12-production-readiness.md 2단계), 대신
    다음 세 신호를 조합해 "구조가 그럴듯한가"를 어림잡는다.

    - `consistent`(가중치 0.3): 전체 행 중 최빈 열 개수와 일치하는 행의 비율 — 열이 들쭉날쭉
      하면 구조 인식이 깨졌을 가능성이 높다.
    - `density`(가중치 0.6): 전체 셀 중 비어 있지 않은 셀의 비율 — 셀은 만들어졌지만 텍스트가
      비어 있다면(OCR 실패) 구조만 있고 내용이 없는 것이므로 가장 큰 가중치를 둔다.
    - `row_score`(가중치 0.1): 행 수가 3행 이상이면 만점, 그 미만이면 비례 축소 — 표가
      너무 짧으면(1~2행) 우연히 일관돼 보여도 신뢰도가 낮다고 보는 보정.

    가중치(0.3/0.6/0.1)와 "3행 기준"은 실측 근거 문서 없음 — 추정값이다. 이 점수는
    `_recognize_table`에서 wired/wireless 중 어느 모델 결과를 채택할지 비교하는 데 쓰이고,
    `review/service.py._automation_quality`에서도 재사용되어 표가 자동 품질 게이트를
    통과할 최소 기준(`paddle_table_min_structure_quality`)으로 쓰인다.
    """
    rows = [
        [cell.strip() for cell in line.split("|")]
        for line in table_text.splitlines()
        if "|" in line
    ]
    if len(rows) < 2:
        return 0.0
    column_counts = [len(row) for row in rows]
    expected_columns = max(set(column_counts), key=column_counts.count)
    if expected_columns < 2:
        return 0.0
    consistent = sum(count == expected_columns for count in column_counts) / len(rows)
    nonempty = sum(bool(cell) for row in rows for cell in row)
    density = nonempty / (len(rows) * expected_columns)
    row_score = min(1.0, len(rows) / 3.0)
    return 0.3 * consistent + 0.6 * density + 0.1 * row_score
