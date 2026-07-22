import os
from pathlib import Path

from pdf_fixtures import PdfBuilder
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
    _recover_author_block_types,
    _recover_page_title_region,
    _render_pdf_pages,
    _reconcile_layout_with_text,
    _scale_blocks,
    _split_inline_abstract_headings,
    _split_merged_section_regions,
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
    (
        PdfBuilder()
        .add_page(400, 500)
        .text(20, 30, "Digital text layer")
        .add_page(400, 500)
        .text(20, 30, "Second page")
        .save(pdf_path)
    )

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


def test_table_colspan_header_keeps_data_columns_aligned() -> None:
    """실측 재현 버그(2026-07-22, LiLT paper_id=3 표 1): colspan을 읽지 않으면

    2열을 차지하는 헤더 셀 뒤로 모든 데이터 열이 한 칸씩 밀려 값이 엉뚱한 열에
    쓰였다. colspan만큼 같은 텍스트를 여러 열에 채워 데이터 행과 정렬을 맞춘다.
    """
    html = (
        "<table>"
        "<tr><td>#</td><td colspan=\"2\">Model Comparison</td></tr>"
        "<tr><td>1</td><td>CAT</td><td>0.6751</td></tr>"
        "</table>"
    )
    assert _html_table_to_pipe_text(html) == (
        "# | Model Comparison | Model Comparison\n1 | CAT | 0.6751"
    )


def test_table_rowspan_cell_repeats_across_spanned_rows() -> None:
    """rowspan=2인 첫 열 셀은 다음 행에서도 같은 값으로 채워져야 나머지 열과 정렬이 맞는다."""
    html = (
        "<table>"
        "<tr><td rowspan=\"2\">Group A</td><td>x</td><td>1</td></tr>"
        "<tr><td>y</td><td>2</td></tr>"
        "<tr><td>Group B</td><td>z</td><td>3</td></tr>"
        "</table>"
    )
    assert _html_table_to_pipe_text(html) == (
        "Group A | x | 1\nGroup A | y | 2\nGroup B | z | 3"
    )


def test_table_literal_pipe_in_cell_is_escaped_to_avoid_column_split() -> None:
    """셀 값 안의 리터럴 "|"(OCR 오인식 등)가 열 구분자로 오인되지 않도록 치환한다."""
    html = "<table><tr><td>a|b</td><td>c</td></tr></table>"
    assert _html_table_to_pipe_text(html) == "a/b | c"


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


def test_layout_dedup_removes_single_text_block_contained_by_text_region() -> None:
    blocks = [
        LayoutBlock(
            page=1,
            block_type="text",
            text="",
            order=0,
            bbox=(80, 120, 500, 200),
        ),
        LayoutBlock(
            page=1,
            block_type="text",
            text="",
            order=1,
            bbox=(200, 135, 410, 150),
        ),
    ]

    deduplicated = deduplicate_layout_blocks(blocks)

    assert deduplicated == [blocks[0]]


def test_layout_dedup_preserves_fallback_text_over_non_text_regions() -> None:
    blocks = [
        LayoutBlock(
            page=1,
            block_type="text",
            text="",
            order=0,
            bbox=(20, 20, 500, 300),
        ),
        LayoutBlock(
            page=1,
            block_type="figure",
            text="",
            order=1,
            bbox=(40, 40, 180, 120),
        ),
        LayoutBlock(
            page=1,
            block_type="formula",
            text="",
            order=2,
            bbox=(220, 160, 360, 210),
        ),
    ]

    deduplicated = deduplicate_layout_blocks(blocks)

    assert blocks[0] in deduplicated


def test_reconciliation_recovers_text_removed_as_layout_container() -> None:
    blocks = [
        LayoutBlock(
            page=1,
            block_type="text",
            text="",
            order=0,
            bbox=(20, 40, 180, 80),
        ),
        LayoutBlock(
            page=1,
            block_type="text",
            text="",
            order=1,
            bbox=(220, 40, 380, 80),
        ),
    ]
    text_lines = [((10.0, 20.0, 400.0, 100.0), 0.97)]

    reconciled, metrics = _reconcile_layout_with_text(
        blocks,
        text_lines,
        page=1,
        start_order=0,
        coverage_threshold=0.8,
        merge_gap_ratio=1.8,
    )

    assert metrics["finally_covered_text_lines"] == 1
    assert any(block.bbox == text_lines[0][0] for block in reconciled)


def test_layout_dedup_removes_truncated_section_header_inside_full_header() -> None:
    blocks = [
        LayoutBlock(
            page=1,
            block_type="section_header",
            text="3.3\nComparisons with the SOTAs",
            order=0,
            bbox=(68, 674, 234, 686),
        ),
        LayoutBlock(
            page=1,
            block_type="section_header",
            text="parisons with the SOI",
            order=1,
            bbox=(116, 674, 219, 683),
        ),
    ]

    deduplicated = deduplicate_layout_blocks(blocks)

    assert deduplicated == [blocks[0]]


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
        "finally_covered_text_lines": 4,
        "expanded_blocks": 1,
        "added_text_blocks": 1,
    }
    assert len(reconciled) == 3


def test_splits_abstract_heading_from_abstract_body_lines() -> None:
    blocks = [
        LayoutBlock(
            page=1,
            block_type="abstract",
            text="",
            order=0,
            bbox=(100, 100, 500, 260),
            confidence=0.8,
        )
    ]
    text_lines = [
        ((240.0, 105.0, 350.0, 125.0), 0.98),
        ((110.0, 140.0, 490.0, 160.0), 0.97),
        ((110.0, 170.0, 480.0, 190.0), 0.96),
        ((110.0, 200.0, 470.0, 220.0), 0.95),
    ]

    split, count = _split_merged_section_regions(
        blocks,
        text_lines,
        page=1,
        start_order=0,
        enabled=True,
        min_body_lines=2,
        max_heading_width_ratio=0.72,
        line_overlap=0.6,
    )

    assert count == 1
    assert [block.block_type for block in split] == ["section_header", "abstract"]
    assert split[0].bbox == text_lines[0][0]
    assert split[1].bbox == (110.0, 140.0, 490.0, 220.0)


def test_splits_body_lines_merged_into_section_header() -> None:
    blocks = [
        LayoutBlock(
            page=2,
            block_type="section_header",
            text="",
            order=4,
            bbox=(60, 80, 520, 220),
        )
    ]
    text_lines = [
        ((70.0, 90.0, 210.0, 110.0), 0.99),
        ((70.0, 130.0, 510.0, 150.0), 0.98),
        ((70.0, 160.0, 500.0, 180.0), 0.97),
    ]

    split, count = _split_merged_section_regions(
        blocks,
        text_lines,
        page=2,
        start_order=4,
        enabled=True,
        min_body_lines=2,
        max_heading_width_ratio=0.72,
        line_overlap=0.6,
    )

    assert count == 1
    assert [block.block_type for block in split] == ["section_header", "text"]
    assert split[0].bbox == text_lines[0][0]
    assert split[1].bbox == (70.0, 130.0, 510.0, 180.0)


def test_does_not_split_full_width_first_abstract_line() -> None:
    blocks = [
        LayoutBlock(
            page=1,
            block_type="abstract",
            text="",
            order=0,
            bbox=(100, 100, 500, 220),
        )
    ]
    text_lines = [
        ((110.0, 110.0, 490.0, 130.0), 0.98),
        ((110.0, 140.0, 480.0, 160.0), 0.97),
        ((110.0, 170.0, 470.0, 190.0), 0.96),
    ]

    split, count = _split_merged_section_regions(
        blocks,
        text_lines,
        page=1,
        start_order=0,
        enabled=True,
        min_body_lines=2,
        max_heading_width_ratio=0.72,
        line_overlap=0.6,
    )

    assert count == 0
    assert split == blocks


def test_splits_inline_abstract_heading_from_first_body_line() -> None:
    blocks = [
        LayoutBlock(
            page=1,
            block_type="abstract",
            text="",
            order=0,
            bbox=(80, 100, 500, 220),
        )
    ]
    first_line = (82.0, 105.0, 498.0, 125.0)
    text_lines = [
        (first_line, 0.98),
        ((82.0, 135.0, 495.0, 155.0), 0.97),
        ((82.0, 165.0, 490.0, 185.0), 0.96),
    ]

    split, count = _split_inline_abstract_headings(
        blocks,
        text_lines,
        {first_line: "Abstract This paper proposes a layout model."},
        page=1,
        start_order=0,
        min_body_lines=2,
        line_overlap=0.6,
        max_prefix_ratio=0.4,
        include_text_blocks=False,
    )

    assert count == 1
    assert [block.block_type for block in split] == [
        "section_header",
        "abstract",
        "abstract",
    ]
    assert split[0].bbox is not None
    assert split[0].bbox[2] == split[1].bbox[0]
    assert split[2].bbox == (82.0, 135.0, 495.0, 185.0)


def test_inline_abstract_split_requires_recognized_heading_prefix() -> None:
    blocks = [
        LayoutBlock(
            page=1,
            block_type="abstract",
            text="",
            order=0,
            bbox=(80, 100, 500, 220),
        )
    ]
    first_line = (82.0, 105.0, 498.0, 125.0)
    text_lines = [
        (first_line, 0.98),
        ((82.0, 135.0, 495.0, 155.0), 0.97),
        ((82.0, 165.0, 490.0, 185.0), 0.96),
    ]

    split, count = _split_inline_abstract_headings(
        blocks,
        text_lines,
        {first_line: "This paper proposes a layout model."},
        page=1,
        start_order=0,
        min_body_lines=2,
        line_overlap=0.6,
        max_prefix_ratio=0.4,
        include_text_blocks=False,
    )

    assert count == 0
    assert split == blocks


def test_recovers_first_page_abstract_misclassified_as_text() -> None:
    blocks = [
        LayoutBlock(
            page=1,
            block_type="text",
            text="",
            order=0,
            bbox=(80, 100, 500, 220),
        )
    ]
    heading_line = (220.0, 105.0, 320.0, 125.0)
    text_lines = [
        (heading_line, 0.98),
        ((82.0, 135.0, 495.0, 155.0), 0.97),
        ((82.0, 165.0, 490.0, 185.0), 0.96),
    ]

    split, count = _split_inline_abstract_headings(
        blocks,
        text_lines,
        {heading_line: "Abstract"},
        page=1,
        start_order=0,
        min_body_lines=2,
        line_overlap=0.6,
        max_prefix_ratio=0.4,
        include_text_blocks=True,
    )

    assert count == 1
    assert [block.block_type for block in split] == ["section_header", "abstract"]


def test_recovers_wide_first_page_title_and_horizontal_author_regions() -> None:
    blocks = [
        LayoutBlock(
            page=1,
            block_type="text",
            text="Paper title",
            order=0,
            bbox=(80, 80, 520, 115),
        ),
        LayoutBlock(
            page=1,
            block_type="text",
            text="First Author",
            order=1,
            bbox=(140, 130, 260, 150),
        ),
        LayoutBlock(
            page=1,
            block_type="text",
            text="vertical marker",
            order=2,
            bbox=(20, 120, 40, 400),
        ),
        LayoutBlock(
            page=1,
            block_type="section_header",
            text="Abstract",
            order=3,
            bbox=(240, 220, 330, 240),
        ),
    ]
    text_lines = [
        ((20.0, 80.0, 40.0, 400.0), 0.9),
        ((80.0, 80.0, 520.0, 115.0), 0.99),
        ((140.0, 130.0, 260.0, 150.0), 0.98),
    ]

    recovered, title_count = _recover_page_title_region(
        blocks,
        text_lines,
        page=1,
        enabled=True,
        min_width_ratio=0.55,
    )
    recovered, author_count = _recover_author_block_types(recovered, enabled=True)

    assert title_count == 1
    assert author_count == 1
    assert [block.block_type for block in recovered] == [
        "title",
        "author",
        "text",
        "section_header",
    ]


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


def test_abstract_header_does_not_turn_following_abstract_into_body() -> None:
    blocks = [
        LayoutBlock(page=1, block_type="title", text="Paper", order=0),
        LayoutBlock(page=1, block_type="section_header", text="Abstract", order=1),
        LayoutBlock(page=1, block_type="abstract", text="Summary", order=2),
        LayoutBlock(page=1, block_type="section_header", text="Introduction", order=3),
        LayoutBlock(page=1, block_type="text", text="Body", order=4),
    ]

    normalized = _normalize_semantic_block_types(blocks)

    assert [block.block_type for block in normalized] == [
        "title",
        "section_header",
        "abstract",
        "section_header",
        "text",
    ]


def test_text_between_first_page_title_and_abstract_is_recovered_as_author() -> None:
    blocks = [
        LayoutBlock(page=1, block_type="title", text="Paper title", order=0),
        LayoutBlock(page=1, block_type="text", text="First Author", order=1),
        LayoutBlock(page=1, block_type="text", text="Example University", order=2),
        LayoutBlock(page=1, block_type="abstract", text="Abstract", order=3),
        LayoutBlock(page=1, block_type="text", text="Body", order=4),
        LayoutBlock(page=2, block_type="text", text="Page two", order=5),
    ]

    normalized = _normalize_semantic_block_types(blocks)

    assert [block.block_type for block in normalized] == [
        "title",
        "author",
        "author",
        "abstract",
        "text",
        "text",
    ]


def test_author_recovery_requires_a_semantic_boundary() -> None:
    blocks = [
        LayoutBlock(page=1, block_type="title", text="Paper title", order=0),
        LayoutBlock(page=1, block_type="text", text="Body", order=1),
    ]

    normalized = _normalize_semantic_block_types(blocks)

    assert [block.block_type for block in normalized] == ["title", "text"]
