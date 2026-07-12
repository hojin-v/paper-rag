import os
from pathlib import Path

import pymupdf
import pytest

from paperrag.config import Settings
from paperrag.ingest.layout.dedup import deduplicate_layout_blocks
from paperrag.ingest.layout.paddle_backend import (
    PaddleBackend,
    _best_table_classification,
    _configure_paddle_runtime,
    _html_table_to_pipe_text,
    _map_layout_detection_result,
    _map_text_detection_result,
    _map_result,
    _normalize_semantic_block_types,
    _render_pdf_pages,
    _reconcile_layout_with_text,
    _scale_blocks,
    _scaled_crop_box,
    _table_structure_quality,
)
from paperrag.ingest.models import LayoutBlock


def test_maps_ppstructure_result_to_layout_blocks() -> None:
    blocks = _map_result(
        {
            "parsing_res_list": [
                {
                    "block_label": "doc_title",
                    "block_content": "논문 제목",
                    "block_bbox": [10, 20, 200, 60],
                    "score": 0.97,
                },
                {
                    "block_label": "table",
                    "block_content": "| A | B |",
                    "block_bbox": [20, 100, 300, 250],
                },
            ]
        },
        page=1,
    )

    assert [block.block_type for block in blocks] == ["title", "table"]
    assert blocks[0].bbox == (10.0, 20.0, 200.0, 60.0)
    assert blocks[0].confidence == 0.97
    assert blocks[0].ocr_engine == "pp-structurev3"


def test_maps_paddleocr_37_result_wrapper() -> None:
    class Result:
        json = {
            "res": {
                "parsing_res_list": [
                    {
                        "block_label": "paragraph_title",
                        "block_content": "1. Introduction",
                        "block_bbox": [20, 40, 220, 70],
                    },
                    {
                        "block_label": "reference_content",
                        "block_content": "A. Author, 2026.",
                        "block_bbox": [20, 100, 300, 130],
                    },
                ]
            }
        }

    blocks = _map_result(Result(), page=1)

    assert [block.block_type for block in blocks] == ["section_header", "reference"]


def test_table_without_structure_model_keeps_coordinate_ocr_text() -> None:
    blocks = _map_result(
        {
            "parsing_res_list": [
                {
                    "block_label": "table",
                    "block_content": "",
                    "block_bbox": [90, 240, 550, 410],
                }
            ],
            "overall_ocr_res": {
                "rec_texts": ["Metric", "Value", "Accuracy", "0.91"],
                "rec_boxes": [
                    [100, 260, 180, 290],
                    [320, 260, 380, 290],
                    [100, 320, 190, 350],
                    [320, 320, 370, 350],
                ],
            },
        },
        page=1,
    )

    assert len(blocks) == 1
    assert blocks[0].block_type == "table"
    assert blocks[0].text == "Metric | Value\nAccuracy | 0.91"


def test_unassigned_full_page_ocr_is_preserved_as_fallback_text() -> None:
    blocks = _map_result(
        {
            "parsing_res_list": [
                {
                    "block_label": "paragraph_title",
                    "block_content": "INTRODUCTION",
                    "block_bbox": [20, 100, 200, 130],
                }
            ],
            "overall_ocr_res": {
                "rec_texts": [
                    "INTRODUCTION",
                    "A body line missed by layout detection.",
                    "A second missed body line.",
                ],
                "rec_boxes": [
                    [20, 100, 200, 130],
                    [20, 145, 500, 170],
                    [20, 175, 480, 200],
                ],
                "rec_scores": [0.99, 0.96, 0.94],
            },
        },
        page=1,
    )

    assert [block.block_type for block in blocks] == [
        "section_header",
        "text",
        "text",
    ]
    assert [block.text for block in blocks[1:]] == [
        "A body line missed by layout detection.",
        "A second missed body line.",
    ]
    assert all(block.ocr_engine == "pp-ocrv5-unassigned" for block in blocks[1:])


def test_overlapping_full_page_ocr_is_not_duplicated() -> None:
    blocks = _map_result(
        {
            "parsing_res_list": [
                {
                    "block_label": "doc_title",
                    "block_content": "Complete title from overlapping OCR",
                    "block_bbox": [100, 40, 400, 90],
                }
            ],
            "overall_ocr_res": {
                "rec_texts": ["Complete title from overlapping OCR"],
                "rec_boxes": [[20, 50, 520, 80]],
                "rec_scores": [0.98],
            },
        },
        page=1,
    )

    assert len(blocks) == 1
    assert blocks[0].block_type == "title"


def test_confidence_is_joined_from_layout_detection_result() -> None:
    blocks = _map_result(
        {
            "parsing_res_list": [
                {
                    "block_label": "doc_title",
                    "block_content": "Title",
                    "block_bbox": [10, 20, 200, 60],
                }
            ],
            "layout_det_res": {
                "boxes": [
                    {
                        "label": "doc_title",
                        "score": 0.87,
                        "coordinate": [9, 19, 201, 61],
                    }
                ]
            },
        },
        page=1,
    )

    assert blocks[0].confidence == 0.87


def test_renders_every_pdf_page_before_ocr_and_restores_pdf_coordinates(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "digital.pdf"
    document = pymupdf.open()
    document.new_page(width=400, height=500).insert_text((20, 30), "Digital text layer")
    document.new_page(width=400, height=500).insert_text((20, 30), "Second page")
    document.save(pdf_path)
    document.close()

    pages = _render_pdf_pages(pdf_path, tmp_path, dpi=144)
    scaled = _scale_blocks(
        _map_result(
            {
                "parsing_res_list": [
                    {
                        "block_label": "text",
                        "block_content": "OCR",
                        "block_bbox": [20, 40, 200, 120],
                    }
                ]
            },
            page=1,
        ),
        pages[0][2],
    )

    assert len(pages) == 2
    assert all(path.is_file() for _, path, _ in pages)
    assert pages[0][2] == 2.0
    assert scaled[0].bbox == (10.0, 20.0, 100.0, 60.0)


def test_layout_only_mapping_has_no_ocr_and_orders_two_columns() -> None:
    blocks = _map_layout_detection_result(
        {
            "boxes": [
                {"label": "text", "coordinate": [320, 180, 560, 240]},
                {"label": "text", "coordinate": [40, 180, 280, 240]},
                {"label": "doc_title", "coordinate": [80, 30, 520, 80]},
                {"label": "text", "coordinate": [320, 100, 560, 160]},
                {"label": "text", "coordinate": [40, 100, 280, 160]},
            ]
        },
        page=1,
    )

    assert [block.block_type for block in blocks] == [
        "title",
        "text",
        "text",
        "text",
        "text",
    ]
    assert [block.bbox[0] for block in blocks[1:] if block.bbox is not None] == [
        40.0,
        40.0,
        320.0,
        320.0,
    ]
    assert all(block.text == "" and block.ocr_engine is None for block in blocks)
    assert [block.order for block in blocks] == list(range(5))


def test_table_html_and_crop_coordinates_are_deterministic() -> None:
    assert _html_table_to_pipe_text(
        "<table><tr><th>Metric</th><th>Value</th></tr>"
        "<tr><td>F1</td><td>0.90</td></tr></table>"
    ) == "Metric | Value\nF1 | 0.90"
    assert _scaled_crop_box((10.1, 20.2, 100.4, 60.6), 2.0, (300, 200)) == (
        20,
        40,
        201,
        122,
    )


def test_region_ocr_configures_cpu_runtime_without_layout_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FLAGS_use_mkldnn", raising=False)
    monkeypatch.delenv("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", raising=False)

    _configure_paddle_runtime(Settings(_env_file=None, paddle_enable_mkldnn=False))

    assert os.environ["FLAGS_use_mkldnn"] == "0"
    assert os.environ["PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT"] == "False"


def test_layout_dedup_prefers_semantic_region_and_removes_large_container() -> None:
    blocks = [
        LayoutBlock(
            page=1,
            block_type="section_header",
            text="",
            order=0,
            bbox=(20, 20, 180, 45),
            confidence=0.7,
        ),
        LayoutBlock(
            page=1,
            block_type="text",
            text="",
            order=1,
            bbox=(20, 20, 180, 45),
            confidence=0.9,
        ),
        LayoutBlock(
            page=1,
            block_type="abstract",
            text="",
            order=2,
            bbox=(10, 60, 300, 300),
            confidence=0.8,
        ),
        LayoutBlock(
            page=1,
            block_type="text",
            text="",
            order=3,
            bbox=(20, 80, 280, 140),
            confidence=0.7,
        ),
        LayoutBlock(
            page=1,
            block_type="text",
            text="",
            order=4,
            bbox=(20, 160, 280, 220),
            confidence=0.7,
        ),
    ]

    deduplicated = deduplicate_layout_blocks(blocks)

    assert [block.block_type for block in deduplicated] == [
        "section_header",
        "text",
        "text",
    ]


def test_table_classifier_routes_wired_and_wireless_labels() -> None:
    assert _best_table_classification(
        {
            "label_names": ["wireless_table", "wired_table"],
            "scores": [0.1, 0.9],
        }
    ) == ("wired", 0.9)
    assert _best_table_classification(
        {
            "label_names": ["wireless_table", "wired_table"],
            "scores": [0.8, 0.2],
        }
    ) == ("wireless", 0.8)


def test_table_structure_quality_rejects_sparse_misaligned_result() -> None:
    good = "Model | FUNSD | CORD\nBERT | 0.60 | 0.89\nLayoutLM | 0.84 | 0.96"
    sparse = "Model BERT |  | FUNSD\nLayoutLM |  | 0.84\n |  | "

    assert _table_structure_quality(good) > 0.9
    assert _table_structure_quality(sparse) < 0.7


def test_table_recognition_tries_alternate_model_when_primary_is_sparse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClassifier:
        def predict(self, path: str) -> list[dict[str, object]]:
            return [
                {
                    "label_names": ["wired_table", "wireless_table"],
                    "scores": [0.9, 0.1],
                }
            ]

    backend = PaddleBackend(
        Settings(_env_file=None, paddle_table_min_structure_quality=0.7)
    )
    backend._table_classifier = FakeClassifier()
    good = "Model | FUNSD | CORD\nBERT | 0.60 | 0.89\nLayoutLM | 0.84 | 0.96"
    sparse = "Model BERT |  | FUNSD\nLayoutLM |  | 0.84\n |  | "
    monkeypatch.setattr(
        backend,
        "_table_text_for_kind",
        lambda path, kind: sparse if kind == "wired" else good,
    )

    text, kind = backend._recognize_table(tmp_path / "table.png")

    assert kind == "wireless"
    assert text == good


def test_text_detector_mapping_and_layout_reconciliation() -> None:
    text_lines = _map_text_detection_result(
        {
            "dt_polys": [
                [[20, 50], [520, 50], [520, 80], [20, 80]],
                [[60, 170], [240, 170], [240, 190], [60, 190]],
                [[300, 160], [500, 160], [500, 180], [300, 180]],
                [[300, 185], [500, 185], [500, 205], [300, 205]],
            ],
            "dt_scores": [0.98, 0.97, 0.96, 0.95],
        }
    )
    blocks = [
        LayoutBlock(
            page=1,
            block_type="title",
            text="",
            order=0,
            bbox=(100, 40, 400, 90),
        ),
        LayoutBlock(
            page=1,
            block_type="text",
            text="",
            order=1,
            bbox=(50, 150, 250, 250),
        ),
    ]

    reconciled, metrics = _reconcile_layout_with_text(
        blocks,
        text_lines,
        page=1,
        start_order=0,
        coverage_threshold=0.8,
        merge_gap_ratio=1.8,
    )

    title = next(block for block in reconciled if block.block_type == "title")
    assert title.bbox == (20.0, 40, 520.0, 90)
    assert metrics == {
        "detected_text_lines": 4,
        "initially_covered_text_lines": 1,
        "expanded_blocks": 1,
        "added_text_blocks": 1,
    }
    assert len(reconciled) == 3


def test_overlapping_partial_titles_are_unioned() -> None:
    blocks = deduplicate_layout_blocks(
        [
            LayoutBlock(
                page=1,
                block_type="title",
                text="",
                order=0,
                bbox=(150, 60, 360, 100),
                confidence=0.4,
            ),
            LayoutBlock(
                page=1,
                block_type="title",
                text="",
                order=1,
                bbox=(215, 60, 425, 100),
                confidence=0.6,
            ),
        ]
    )

    assert len(blocks) == 1
    assert blocks[0].bbox == (150, 60, 425, 100)
    assert blocks[0].confidence == 0.6


def test_adjacent_unassigned_text_expands_abstract_instead_of_body_fallback() -> None:
    blocks = [
        LayoutBlock(
            page=1,
            block_type="abstract",
            text="",
            order=0,
            bbox=(100, 100, 300, 300),
        )
    ]
    text_lines = [((100.0, 70.0, 300.0, 90.0), 0.95)]

    reconciled, metrics = _reconcile_layout_with_text(
        blocks,
        text_lines,
        page=1,
        start_order=0,
        coverage_threshold=0.8,
        merge_gap_ratio=1.8,
        abstract_merge_gap_ratio=0.5,
        abstract_merge_x_overlap=0.7,
    )

    assert len(reconciled) == 1
    assert reconciled[0].block_type == "abstract"
    assert reconciled[0].bbox == (100, 70.0, 300, 300)
    assert metrics["expanded_blocks"] == 1
    assert metrics["added_text_blocks"] == 0


def test_abstract_detected_after_section_is_treated_as_body_without_reordering() -> None:
    blocks = [
        LayoutBlock(page=1, block_type="title", text="", order=0),
        LayoutBlock(page=1, block_type="abstract", text="", order=1),
        LayoutBlock(page=1, block_type="section_header", text="", order=2),
        LayoutBlock(page=1, block_type="text", text="", order=3),
        LayoutBlock(page=1, block_type="abstract", text="", order=4),
        LayoutBlock(page=1, block_type="text", text="", order=5),
    ]

    normalized = _normalize_semantic_block_types(blocks)

    assert [block.block_type for block in normalized] == [
        "title",
        "abstract",
        "section_header",
        "text",
        "text",
        "text",
    ]
    assert [block.order for block in normalized] == list(range(6))
