import os
from collections.abc import Iterable, Mapping
from html.parser import HTMLParser
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from typing import Any

from paperrag.config import Settings, get_settings
from paperrag.ingest.layout.dedup import deduplicate_layout_blocks
from paperrag.ingest.models import BlockType, DocumentLayout, LayoutBlock

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


class PaddleBackend:
    """PP-StructureV3 결과를 공통 LayoutBlock 계약으로 변환한다."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._table_classifier: Any | None = None
        self._table_pipelines: dict[str, Any] = {}

    def analyze(self, pdf_path: str) -> DocumentLayout:
        _configure_paddle_runtime(self.settings)
        try:
            from paddleocr import PPStructureV3  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                "PaddleOCR가 설치되어 있지 않습니다. `pip install -e \".[ingest-full]\"` 후 "
                "`scripts/download_paddle_models.py`로 사전학습 모델을 준비하세요."
            ) from exc

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
                    blocks.extend(_scale_blocks(page_blocks, scale))
        return DocumentLayout(
            source_path=str(Path(pdf_path)),
            is_scanned=True,
            blocks=blocks,
        )

    def analyze_layout(self, pdf_path: str) -> DocumentLayout:
        """레이아웃을 검출하고 텍스트 검출 좌표로 누락·잘림을 자동 보정한다."""
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
        metric_totals: dict[str, int] = {
            "detected_text_lines": 0,
            "initially_covered_text_lines": 0,
            "expanded_blocks": 0,
            "added_text_blocks": 0,
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
                    text_lines: list[
                        tuple[tuple[float, float, float, float], float | None]
                    ] = []
                    for result in text_detector.predict(str(image_path)):
                        text_lines.extend(_map_text_detection_result(result))
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
                    for key in metric_totals:
                        metric_totals[key] += int(page_metrics[key])
                blocks.extend(_scale_blocks(page_blocks, scale))
        detected_lines = metric_totals["detected_text_lines"]
        metrics: dict[str, int | float] = {
            **metric_totals,
            "initial_text_coverage": (
                metric_totals["initially_covered_text_lines"] / detected_lines
                if detected_lines
                else 0.0
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
        """확정된 레이아웃 박스를 crop한 뒤 일반 OCR 또는 표 OCR을 실행한다."""
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
                                    "confidence": block.confidence
                                    if block.confidence is not None
                                    else confidence,
                                    "ocr_engine": engine,
                                }
                            )
                        )
        recognized.sort(key=lambda item: item.order)
        recognized = _normalize_semantic_block_types(recognized)
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
        _configure_paddle_runtime(self.settings)
        try:
            from paddlex import create_model  # type: ignore[import-not-found]
        except ImportError:
            return "", None
        classifier_dir = self.settings.paddle_table_classification_model_dir
        if classifier_dir is None or not classifier_dir.is_dir():
            return "", None
        if self._table_classifier is None:
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
        alternate_kind = "wireless" if table_kind == "wired" else "wired"
        alternate_text = self._table_text_for_kind(crop_path, alternate_kind)
        if _table_structure_quality(alternate_text) > primary_quality:
            return alternate_text, alternate_kind
        return primary_text, table_kind

    def _table_text_for_kind(self, crop_path: Path, table_kind: str) -> str:
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
        pipeline = self._table_pipelines.get(table_kind)
        if pipeline is None:
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
        return _html_table_to_pipe_text(str(first.get("pred_html") or ""))


def _map_result(result: Any, *, page: int, start_order: int = 0) -> list[LayoutBlock]:
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
        if not text and block_type == "table" and bbox is not None:
            text = _ocr_text_in_bbox(payload, bbox)
        if not text and block_type not in {"figure", "formula"}:
            continue
        blocks.append(
            LayoutBlock(
                page=_page_number(candidate, page),
                block_type=block_type,
                text=text,
                order=start_order + len(blocks),
                bbox=bbox,
                confidence=_score(candidate) or _layout_score(payload, label, bbox),
                ocr_engine="pp-structurev3",
            )
        )
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
    return _order_detected_blocks(
        deduplicate_layout_blocks(blocks),
        start_order=start_order,
    )


def _map_text_detection_result(
    result: Any,
) -> list[tuple[tuple[float, float, float, float], float | None]]:
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
    updated = list(blocks)
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
        if best_overlap < coverage_threshold:
            unassigned.append((line_bbox, line_score))

    fallback_groups = _merge_unassigned_text_lines(
        unassigned,
        gap_ratio=merge_gap_ratio,
    )
    typical_line_height = median(
        [max(1.0, bbox[3] - bbox[1]) for bbox, _ in text_lines]
    ) if text_lines else 1.0
    added_groups = 0
    for bbox, confidence in fallback_groups:
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
    return reconciled, {
        "detected_text_lines": len(text_lines),
        "initially_covered_text_lines": initially_covered,
        "expanded_blocks": len(expanded_indices),
        "added_text_blocks": added_groups,
    }


def _adjacent_abstract_index(
    blocks: list[LayoutBlock],
    bbox: tuple[float, float, float, float],
    *,
    max_gap: float,
    min_x_overlap: float,
) -> int | None:
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
    if second is None:
        return 0.0
    overlap = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    first_height = max(0.0, first[3] - first[1])
    return overlap / first_height if first_height > 0 else 0.0


def _horizontal_overlap_fraction(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    overlap = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    smaller_width = min(first[2] - first[0], second[2] - second[0])
    return overlap / smaller_width if smaller_width > 0 else 0.0


def _bbox_union(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
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
    positioned = [block for block in blocks if block.bbox is not None]
    if len(positioned) < 2:
        return blocks
    left_edge = min(block.bbox[0] for block in positioned if block.bbox is not None)
    right_edge = max(block.bbox[2] for block in positioned if block.bbox is not None)
    content_width = right_edge - left_edge
    midpoint = (left_edge + right_edge) / 2.0
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
        ordered = sorted(positioned, key=_top_left_key)
    else:
        spanning = sorted(
            (block for block in positioned if spans_columns(block)),
            key=_top_left_key,
        )
        bands: list[list[LayoutBlock]] = [[] for _ in range(len(spanning) + 1)]
        for block in positioned:
            if spans_columns(block):
                continue
            center_y = (block.bbox[1] + block.bbox[3]) / 2.0
            band_index = sum(
                span.bbox is not None and span.bbox[3] <= center_y for span in spanning
            )
            bands[band_index].append(block)
        ordered = []
        for index, band in enumerate(bands):
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
    unpositioned = [block for block in blocks if block.bbox is None]
    ordered.extend(unpositioned)
    return [
        block.model_copy(update={"order": start_order + index})
        for index, block in enumerate(ordered)
    ]


def _normalize_semantic_block_types(blocks: list[LayoutBlock]) -> list[LayoutBlock]:
    body_started_by_page: dict[int, bool] = {}
    normalized: list[LayoutBlock] = []
    for block in sorted(blocks, key=lambda item: item.order):
        body_started = body_started_by_page.get(block.page, False)
        if block.block_type == "section_header":
            body_started_by_page[block.page] = True
            normalized.append(block)
            continue
        if block.block_type == "abstract" and body_started:
            normalized.append(block.model_copy(update={"block_type": "text"}))
            continue
        normalized.append(block)
    return normalized


def _top_left_key(block: LayoutBlock) -> tuple[float, float]:
    if block.bbox is None:
        return (float("inf"), float("inf"))
    return (block.bbox[1], block.bbox[0])


def _as_mapping(value: Any) -> Mapping[str, Any]:
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
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _normalize_label(label: str) -> str:
    return label.strip().lower().replace("-", "_").replace(" ", "_")


def _bbox(row: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
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
    value = row.get("score", row.get("confidence", row.get("rec_score")))
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def _page_number(row: Mapping[str, Any], fallback: int) -> int:
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
        if normalized and x1 <= center_x <= x2 and y1 <= center_y <= y2:
            cells.append((center_y, center_x, max(1.0, by2 - by1), normalized))
    if not cells:
        return ""

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
    overlap_width = min(text_bbox[2], layout_bbox[2]) - max(text_bbox[0], layout_bbox[0])
    overlap_height = min(text_bbox[3], layout_bbox[3]) - max(text_bbox[1], layout_bbox[1])
    return overlap_width > 3.0 and overlap_height > 3.0


def _merge_in_reading_order(
    blocks: list[LayoutBlock],
    fallback_blocks: list[LayoutBlock],
    *,
    start_order: int,
) -> list[LayoutBlock]:
    all_blocks = [*blocks, *fallback_blocks]
    bboxes = [block.bbox for block in all_blocks if block.bbox is not None]
    if not bboxes:
        return all_blocks
    page_width = max(bbox[2] for bbox in bboxes)
    midpoint = page_width / 2.0

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
    if dpi <= 0:
        raise ValueError("PAPERRAG_OCR_RENDER_DPI must be positive.")
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError("전체 PDF OCR에는 PyMuPDF 페이지 렌더링이 필요합니다.") from exc

    scale = dpi / 72.0
    pages: list[tuple[int, Path, float]] = []
    with pymupdf.open(pdf_path) as document:
        for page_index, page in enumerate(document, start=1):
            image_path = output_dir / f"page-{page_index:05d}.png"
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            pixmap.save(image_path)
            pages.append((page_index, image_path, scale))
    return pages


def _scale_blocks(blocks: list[LayoutBlock], scale: float) -> list[LayoutBlock]:
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
    if not path.is_dir() or not any(item.is_file() for item in path.rglob("*")):
        raise FileNotFoundError(f"{setting_name}={path} 디렉터리가 없거나 비어 있습니다.")


def _configure_paddle_runtime(settings: Settings) -> None:
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(settings.paddlex_cache_dir))
    if not settings.paddle_enable_mkldnn:
        os.environ.setdefault("FLAGS_use_mkldnn", "0")
        os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "False")


def _scaled_crop_box(
    bbox: tuple[float, float, float, float],
    scale: float,
    image_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    width, height = image_size
    x1, y1, x2, y2 = bbox
    return (
        max(0, min(width - 1, int(x1 * scale))),
        max(0, min(height - 1, int(y1 * scale))),
        max(1, min(width, int(x2 * scale + 0.999))),
        max(1, min(height, int(y2 * scale + 0.999))),
    )


class _TableHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            normalized = " ".join(data.split())
            if normalized:
                self._cell.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _html_table_to_pipe_text(html: str) -> str:
    if not html.strip():
        return ""
    parser = _TableHtmlParser()
    parser.feed(html)
    return "\n".join(" | ".join(row) for row in parser.rows)


def _table_structure_quality(table_text: str) -> float:
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
