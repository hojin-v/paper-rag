from paperrag.ingest.filterer import EXCLUDED, split_blocks
from paperrag.ingest.models import LayoutBlock
from paperrag.config import Settings


def test_split_blocks_filters_all_excluded_types_and_groups_meta_body_tables() -> None:
    blocks = [
        LayoutBlock(page=1, block_type="title", text="Title", order=1),
        LayoutBlock(page=1, block_type="author", text="A, B", order=2),
        LayoutBlock(page=1, block_type="abstract", text="Abstract", order=3),
        LayoutBlock(page=1, block_type="section_header", text="Intro", order=4),
        LayoutBlock(page=1, block_type="text", text="Body", order=5),
        LayoutBlock(page=1, block_type="table_caption", text="Table 1", order=6),
        LayoutBlock(page=1, block_type="table", text="a | b", order=7),
        LayoutBlock(page=1, block_type="figure", text="figure", order=8),
        LayoutBlock(page=1, block_type="figure_caption", text="figure caption", order=9),
        LayoutBlock(page=1, block_type="formula", text="E=mc2", order=10),
        LayoutBlock(page=1, block_type="header_footer", text="1", order=11),
        LayoutBlock(page=1, block_type="reference", text="ref", order=12),
        LayoutBlock(page=1, block_type="text", text="after reference block", order=13),
    ]

    meta, body, tables = split_blocks(blocks)

    assert set(EXCLUDED) == {"figure", "figure_caption", "formula", "header_footer", "reference"}
    assert [block.text for block in meta["title"]] == ["Title"]
    assert [block.text for block in meta["author"]] == ["A, B"]
    assert [block.text for block in meta["abstract"]] == ["Abstract"]
    assert [block.text for block in body] == ["Intro", "Body"]
    assert [block.text for block in tables] == ["Table 1", "a | b"]


def test_split_blocks_excludes_everything_after_reference_section_header() -> None:
    blocks = [
        LayoutBlock(page=1, block_type="section_header", text="Results", order=1),
        LayoutBlock(page=1, block_type="text", text="result text", order=2),
        LayoutBlock(page=2, block_type="section_header", text="참고문헌", order=3),
        LayoutBlock(page=2, block_type="text", text="must be excluded", order=4),
        LayoutBlock(page=3, block_type="table", text="also excluded", order=5),
    ]

    _, body, tables = split_blocks(blocks)

    assert [block.text for block in body] == ["Results", "result text"]
    assert tables == []


def test_reference_boundary_is_detected_even_when_misclassified_as_text() -> None:
    blocks = [
        LayoutBlock(page=1, block_type="text", text="body", order=1),
        LayoutBlock(page=2, block_type="text", text="REFERENCES", order=2),
        LayoutBlock(page=2, block_type="text", text="[1] excluded", order=3),
    ]

    _, body, _ = split_blocks(blocks)

    assert [block.text for block in body] == ["body"]


def test_figure_caption_text_fallback_is_not_body() -> None:
    blocks = [
        LayoutBlock(page=1, block_type="text", text="body", order=1),
        LayoutBlock(page=1, block_type="text", text="Figure 1. Architecture", order=2),
        LayoutBlock(page=1, block_type="text", text="Fig. 2 Results", order=3),
    ]

    _, body, _ = split_blocks(blocks)

    assert [block.text for block in body] == ["body"]


def test_late_abstract_label_is_kept_in_body_after_section_start() -> None:
    blocks = [
        LayoutBlock(page=1, block_type="abstract", text="real abstract", order=1),
        LayoutBlock(page=1, block_type="section_header", text="1 Introduction", order=2),
        LayoutBlock(page=1, block_type="text", text="left column", order=3),
        LayoutBlock(
            page=1,
            block_type="abstract",
            text="right-column continuation",
            order=4,
        ),
    ]

    meta, body, _ = split_blocks(blocks)

    assert [block.text for block in meta["abstract"]] == ["real abstract"]
    assert [block.text for block in body] == [
        "1 Introduction",
        "left column",
        "right-column continuation",
    ]
    assert body[-1].block_type == "text"


def test_small_bottom_footnote_is_excluded_without_dropping_body_block() -> None:
    blocks = [
        LayoutBlock(
            page=1,
            block_type="text",
            text="body ending with many business",
            order=1,
            bbox=(70, 680, 292, 748),
        ),
        LayoutBlock(
            page=1,
            block_type="text",
            text="Equal contributions during internship",
            order=2,
            bbox=(88, 754, 263, 766),
        ),
        LayoutBlock(
            page=1,
            block_type="text",
            text="applications. Continued body paragraph.",
            order=3,
            bbox=(303, 251, 527, 440),
        ),
        LayoutBlock(
            page=1,
            block_type="header_footer",
            text="Proceedings footer",
            order=4,
            bbox=(123, 798, 471, 827),
        ),
    ]

    _, body, _ = split_blocks(blocks, settings=Settings(_env_file=None))

    assert [block.text for block in body] == [
        "body ending with many business",
        "applications. Continued body paragraph.",
    ]
